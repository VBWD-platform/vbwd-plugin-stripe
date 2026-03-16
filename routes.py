"""Stripe plugin API routes."""
import logging
from decimal import Decimal
from uuid import UUID

from flask import Blueprint, jsonify, request, current_app, g

from src.middleware.auth import require_auth
from src.plugins.payment_route_helpers import (
    check_plugin_enabled,
    validate_invoice_for_payment,
    emit_payment_captured,
    determine_session_mode,
)
from src.sdk.interface import SDKConfig
from src.models.enums import LineItemType, InvoiceStatus
from src.models.invoice_line_item import InvoiceLineItem
from src.models.invoice import UserInvoice
from src.models.subscription import Subscription
from src.models.addon_subscription import AddOnSubscription
from src.events.payment_events import (
    SubscriptionCancelledEvent,
    PaymentFailedEvent,
    PaymentRefundedEvent,
    RefundReversedEvent,
)
from src.extensions import db

logger = logging.getLogger(__name__)

stripe_plugin_bp = Blueprint("stripe_plugin", __name__)

# Billing period to Stripe recurring interval mapping
BILLING_PERIOD_TO_STRIPE = {
    "DAILY": {"interval": "day"},
    "WEEKLY": {"interval": "week"},
    "MONTHLY": {"interval": "month"},
    "QUARTERLY": {"interval": "month", "interval_count": 3},
    "YEARLY": {"interval": "year"},
}


def _get_adapter(config):
    """Instantiate StripeSDKAdapter from plugin config."""
    from plugins.stripe.sdk_adapter import StripeSDKAdapter

    prefix = "test_" if config.get("sandbox", True) else "live_"
    return StripeSDKAdapter(
        SDKConfig(
            api_key=config.get(f"{prefix}secret_key") or config.get("secret_key", ""),
            sandbox=config.get("sandbox", True),
        )
    )


def _build_stripe_subscription_items(invoice):
    """Convert invoice recurring line items to Stripe subscription line_items."""
    items = []
    currency = (invoice.currency or "EUR").lower()

    for li in invoice.line_items:
        if li.item_type == LineItemType.SUBSCRIPTION:
            sub = db.session.get(Subscription, li.item_id)
            if sub and sub.tarif_plan and sub.tarif_plan.is_recurring:
                period = sub.tarif_plan.billing_period.value
                recurring = BILLING_PERIOD_TO_STRIPE.get(period, {"interval": "month"})
                items.append(
                    {
                        "price_data": {
                            "currency": currency,
                            "unit_amount": int(li.unit_price * 100),
                            "recurring": recurring,
                            "product_data": {"name": sub.tarif_plan.name},
                        },
                        "quantity": 1,
                    }
                )
        elif li.item_type == LineItemType.ADD_ON:
            addon_sub = db.session.get(AddOnSubscription, li.item_id)
            if addon_sub and addon_sub.addon and addon_sub.addon.is_recurring:
                period = addon_sub.addon.billing_period
                recurring = BILLING_PERIOD_TO_STRIPE.get(period, {"interval": "month"})
                items.append(
                    {
                        "price_data": {
                            "currency": currency,
                            "unit_amount": int(li.unit_price * 100),
                            "recurring": recurring,
                            "product_data": {"name": addon_sub.addon.name},
                        },
                        "quantity": li.quantity,
                    }
                )
    return items


@stripe_plugin_bp.route("/create-session", methods=["POST"])
@require_auth
def create_session():
    """Create a Stripe Checkout Session for a PENDING invoice."""
    config, err = check_plugin_enabled("stripe")
    if err:
        return err

    data = request.get_json() or {}
    invoice, err = validate_invoice_for_payment(data.get("invoice_id", ""), g.user_id)
    if err:
        return err

    adapter = _get_adapter(config)
    mode = determine_session_mode(invoice)
    base_meta = {"invoice_id": str(invoice.id), "user_id": str(g.user_id)}
    # Use Origin/Referer header to get the frontend URL (handles proxied ports correctly)
    frontend_base = (
        request.headers.get("Origin")
        or request.headers.get("Referer", "").rstrip("/").rsplit("/pay", 1)[0]
        or request.host_url.rstrip("/")
    )
    success_url = (
        f"{frontend_base}/pay/stripe/success?session_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = f"{frontend_base}/pay/stripe/cancel"

    if mode == "subscription":
        # Recurring: get/create Stripe Customer, create subscription session
        container = current_app.container
        user_repo = container.user_repository()
        user = user_repo.find_by_id(g.user_id)
        customer_id = getattr(user, "payment_customer_id", None)
        if not customer_id:
            cust_resp = adapter.create_or_get_customer(email=user.email)
            if not cust_resp.success:
                return jsonify({"error": cust_resp.error}), 500
            customer_id = cust_resp.data["customer_id"]
            user.payment_customer_id = customer_id
            user_repo.save(user)

        line_items = _build_stripe_subscription_items(invoice)
        response = adapter.create_subscription_session(
            customer_id=customer_id,
            line_items=line_items,
            metadata=base_meta,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    else:
        # One-time: standard payment session
        meta = {
            **base_meta,
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        response = adapter.create_payment_intent(
            amount=Decimal(str(invoice.total_amount or invoice.amount)),
            currency=(invoice.currency or "EUR"),
            metadata=meta,
        )

    if not response.success:
        return jsonify({"error": response.error}), 500

    # Store Stripe session ID on invoice for reliable mapping (webhook fallback)
    stripe_session_id = response.data.get("session_id", "")
    if stripe_session_id:
        invoice.provider_session_id = stripe_session_id
        current_app.container.invoice_repository().save(invoice)

    return jsonify(response.data), 200


@stripe_plugin_bp.route("/webhook", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhook events."""
    config, err = check_plugin_enabled("stripe")
    if err:
        return err

    import stripe

    payload = request.get_data()
    signature = request.headers.get("Stripe-Signature")

    prefix = "test_" if config.get("sandbox", True) else "live_"
    secret_key = config.get(f"{prefix}secret_key") or config.get("secret_key", "")
    webhook_secret = config.get(f"{prefix}webhook_secret") or config.get(
        "webhook_secret", ""
    )

    try:
        stripe.api_key = secret_key
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
    except (stripe.error.SignatureVerificationError, ValueError):
        return jsonify({"error": "Invalid signature"}), 400

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(obj)
    elif event_type == "invoice.paid":
        _handle_invoice_paid(obj)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(obj)
    elif event_type == "invoice.payment_failed":
        _handle_payment_failed(obj)
    elif event_type == "charge.refunded":
        _handle_charge_refunded(obj, config)
    elif event_type == "refund.updated":
        _handle_refund_updated(obj, config)

    return jsonify({"received": True}), 200


def _handle_checkout_completed(session):
    """Handle initial checkout.session.completed — emit PaymentCapturedEvent."""
    metadata = session.get("metadata", {})
    invoice_id = metadata.get("invoice_id")
    if not invoice_id:
        return

    # If subscription mode, store provider_subscription_id on our Subscription
    stripe_sub_id = session.get("subscription")
    if stripe_sub_id:
        _link_stripe_subscription(UUID(invoice_id), stripe_sub_id)

    # Event-driven: emit, don't act
    emit_payment_captured(
        invoice_id=UUID(invoice_id),
        payment_reference=session["id"],
        amount=str(session["amount_total"] / 100),
        currency=session.get("currency", "usd"),
        provider="stripe",
        transaction_id=session.get("payment_intent", ""),
    )


def _handle_invoice_paid(stripe_invoice):
    """Handle Stripe invoice.paid — renewal payment for subscriptions."""
    # Skip the first invoice (already handled by checkout.session.completed)
    if stripe_invoice.get("billing_reason") == "subscription_create":
        return

    stripe_sub_id = stripe_invoice.get("subscription")
    if not stripe_sub_id:
        return

    container = current_app.container
    sub_repo = container.subscription_repository()
    subscription = sub_repo.find_by_provider_subscription_id(stripe_sub_id)
    if not subscription:
        return

    # Create renewal invoice in our system
    renewal_invoice = _create_renewal_invoice(subscription, stripe_invoice)

    emit_payment_captured(
        invoice_id=renewal_invoice.id,
        payment_reference=stripe_invoice["id"],
        amount=str(stripe_invoice["amount_paid"] / 100),
        currency=stripe_invoice.get("currency", "usd"),
        provider="stripe",
        transaction_id=stripe_invoice.get("payment_intent", ""),
    )


def _handle_subscription_deleted(stripe_sub):
    """Handle Stripe customer.subscription.deleted — cancel our subscription."""
    container = current_app.container
    sub_repo = container.subscription_repository()
    subscription = sub_repo.find_by_provider_subscription_id(stripe_sub["id"])
    if not subscription:
        return

    event = SubscriptionCancelledEvent(
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        reason="stripe_subscription_deleted",
        provider="stripe",
    )
    container.event_dispatcher().emit(event)


def _handle_payment_failed(stripe_invoice):
    """Handle Stripe invoice.payment_failed — renewal charge failed."""
    stripe_sub_id = stripe_invoice.get("subscription")
    if not stripe_sub_id:
        return

    container = current_app.container
    sub_repo = container.subscription_repository()
    subscription = sub_repo.find_by_provider_subscription_id(stripe_sub_id)
    if not subscription:
        return

    event = PaymentFailedEvent(
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        error_code="payment_failed",
        error_message=stripe_invoice.get("last_payment_error", {}).get(
            "message", "Payment failed"
        )
        if isinstance(stripe_invoice.get("last_payment_error"), dict)
        else "Payment failed",
        provider="stripe",
    )
    container.event_dispatcher().emit(event)


def _handle_charge_refunded(charge, config):
    """Handle Stripe charge.refunded — mark invoice as refunded.

    Traces charge → payment_intent → checkout session → invoice_id.
    """
    payment_intent_id = charge.get("payment_intent")
    if not payment_intent_id:
        return

    # Look up the checkout session that created this charge
    import stripe

    prefix = "test_" if config.get("sandbox", True) else "live_"
    stripe.api_key = config.get(f"{prefix}secret_key") or config.get("secret_key", "")
    try:
        sessions = stripe.checkout.Session.list(
            payment_intent=payment_intent_id, limit=1
        )
    except Exception:
        logger.exception(
            "Failed to look up session for refund PI=%s", payment_intent_id
        )
        return

    if not sessions.data:
        return

    session = sessions.data[0]
    metadata = dict(session.metadata or {})
    invoice_id_str = metadata.get("invoice_id")
    if not invoice_id_str:
        return

    try:
        invoice_id = UUID(invoice_id_str)
    except (ValueError, TypeError):
        return

    refund_amount = charge.get("amount_refunded", 0) / 100
    event = PaymentRefundedEvent(
        invoice_id=invoice_id,
        refund_reference=charge.get("id", ""),
        amount=str(refund_amount),
        currency=charge.get("currency", "usd"),
    )
    container = current_app.container
    container.event_dispatcher().emit(event)
    logger.info(
        "Refund processed for invoice %s, charge %s", invoice_id, charge.get("id")
    )


def _handle_refund_updated(refund_obj, config):
    """Handle Stripe refund.updated — if refund was canceled, restore the invoice.

    Traces refund → charge → payment_intent → checkout session → invoice_id.
    Only acts when refund status becomes 'canceled'.
    """
    if refund_obj.get("status") != "canceled":
        return

    # Get the charge/payment_intent from the refund
    prefix = "test_" if config.get("sandbox", True) else "live_"
    api_key = config.get(f"{prefix}secret_key") or config.get("secret_key", "")
    payment_intent_id = refund_obj.get("payment_intent")
    if not payment_intent_id:
        # Try via charge
        charge_id = refund_obj.get("charge")
        if not charge_id:
            return
        import stripe

        stripe.api_key = api_key
        try:
            charge = stripe.Charge.retrieve(charge_id)
            payment_intent_id = (
                charge.get("payment_intent")
                if isinstance(charge, dict)
                else getattr(charge, "payment_intent", None)
            )
        except Exception:
            logger.exception(
                "Failed to retrieve charge %s for refund reversal", charge_id
            )
            return
        if not payment_intent_id:
            return

    import stripe

    stripe.api_key = api_key
    try:
        sessions = stripe.checkout.Session.list(
            payment_intent=payment_intent_id, limit=1
        )
    except Exception:
        logger.exception(
            "Failed to look up session for refund reversal PI=%s", payment_intent_id
        )
        return

    if not sessions.data:
        return

    session = sessions.data[0]
    metadata = dict(session.metadata or {})
    invoice_id_str = metadata.get("invoice_id")
    if not invoice_id_str:
        return

    try:
        invoice_id = UUID(invoice_id_str)
    except (ValueError, TypeError):
        return

    event = RefundReversedEvent(
        invoice_id=invoice_id,
        reason="stripe_refund_canceled",
        provider="stripe",
    )
    container = current_app.container
    container.event_dispatcher().emit(event)
    logger.info(
        "Refund reversal processed for invoice %s, refund %s",
        invoice_id,
        refund_obj.get("id"),
    )


def _link_stripe_subscription(invoice_id, provider_subscription_id):
    """Store provider_subscription_id on our Subscription after initial checkout."""
    container = current_app.container
    invoice_repo = container.invoice_repository()
    sub_repo = container.subscription_repository()

    invoice = invoice_repo.find_by_id(invoice_id)
    if not invoice:
        return

    for li in invoice.line_items:
        if li.item_type == LineItemType.SUBSCRIPTION:
            subscription = sub_repo.find_by_id(li.item_id)
            if subscription:
                subscription.provider_subscription_id = provider_subscription_id
                sub_repo.save(subscription)
                break


def _create_renewal_invoice(subscription, stripe_invoice):
    """Create a renewal invoice in our system from Stripe's auto-generated invoice."""
    container = current_app.container
    invoice_repo = container.invoice_repository()

    # Deduplication: check if we already processed this Stripe invoice
    existing = invoice_repo.find_by_provider_session_id(stripe_invoice["id"])
    if existing:
        return existing

    plan = subscription.tarif_plan
    amount = Decimal(str(stripe_invoice["amount_paid"] / 100))
    renewal_invoice = UserInvoice(
        user_id=subscription.user_id,
        tarif_plan_id=plan.id if plan else None,
        subscription_id=subscription.id,
        invoice_number=UserInvoice.generate_invoice_number(),
        amount=amount,
        total_amount=amount,
        currency=(stripe_invoice.get("currency", "eur")).upper(),
        status=InvoiceStatus.PENDING,
        payment_method="stripe",
        provider_session_id=stripe_invoice["id"],
    )
    # Add subscription line item
    renewal_invoice.line_items.append(
        InvoiceLineItem(
            item_type=LineItemType.SUBSCRIPTION,
            item_id=subscription.id,
            description=f"Renewal: {plan.name}" if plan else "Subscription renewal",
            quantity=1,
            unit_price=amount,
            total_price=amount,
        )
    )
    invoice_repo.save(renewal_invoice)
    return renewal_invoice


@stripe_plugin_bp.route("/session-status/<session_id>", methods=["GET"])
@require_auth
def session_status(session_id):
    """Poll Stripe Checkout Session status.

    Also performs reconciliation: if Stripe says 'paid' but our invoice
    is still PENDING, emit PaymentCapturedEvent as a webhook fallback.
    This handles cases where the webhook can't reach us (e.g. local dev).
    """
    config, err = check_plugin_enabled("stripe")
    if err:
        return err

    adapter = _get_adapter(config)
    response = adapter.get_payment_status(session_id)
    if not response.success:
        return jsonify({"error": response.error}), 500

    data = response.data

    # Reconciliation: if Stripe says paid, ensure our invoice is updated
    if data.get("status") == "paid":
        _reconcile_payment(data)

    return (
        jsonify(
            {
                "status": data.get("status"),
                "amount_total": data.get("amount_total"),
                "currency": data.get("currency"),
            }
        ),
        200,
    )


def _reconcile_payment(session_data):
    """Emit PaymentCapturedEvent if Stripe says paid but our invoice is still PENDING."""
    metadata = session_data.get("metadata", {})
    invoice_id_str = metadata.get("invoice_id")

    # Fallback: look up invoice by provider_session_id if metadata is empty
    if not invoice_id_str:
        stripe_session_id = session_data.get("session_id", "")
        if stripe_session_id:
            container = current_app.container
            invoice_repo = container.invoice_repository()
            invoice = invoice_repo.find_by_provider_session_id(stripe_session_id)
            if invoice:
                invoice_id_str = str(invoice.id)
    if not invoice_id_str:
        return

    try:
        invoice_id = UUID(invoice_id_str)
    except (ValueError, TypeError):
        return

    container = current_app.container
    invoice_repo = container.invoice_repository()
    invoice = invoice_repo.find_by_id(invoice_id)
    if not invoice or invoice.status != InvoiceStatus.PENDING:
        return

    logger.info(
        "Reconciliation: Stripe session paid but invoice %s still PENDING — emitting event",
        invoice_id,
    )

    # Link stripe subscription if present
    stripe_sub_id = session_data.get("subscription")
    if stripe_sub_id:
        _link_stripe_subscription(invoice_id, stripe_sub_id)

    emit_payment_captured(
        invoice_id=invoice_id,
        payment_reference=session_data.get("session_id", ""),
        amount=str((session_data.get("amount_total") or 0) / 100),
        currency=session_data.get("currency", "usd"),
        provider="stripe",
        transaction_id=session_data.get("payment_intent", ""),
    )
