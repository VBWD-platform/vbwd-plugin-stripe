"""Stripe plugin API routes."""
import logging
from decimal import Decimal
from uuid import UUID

from flask import Blueprint, jsonify, request, current_app, g

from vbwd.middleware.auth import require_auth
from vbwd.plugins.payment_route_helpers import (
    build_provider_redirect_urls,
    check_plugin_enabled,
    determine_session_mode,
    emit_payment_captured,
    publish_provider_cancelled,
    publish_provider_linked,
    publish_recurring_charge,
    publish_recurring_failed,
    validate_invoice_for_payment,
)
from vbwd.sdk.interface import SDKConfig
from vbwd.models.enums import InvoiceStatus
from vbwd.events.payment_events import (
    PaymentRefundedEvent,
    RefundReversedEvent,
)
from vbwd.events.line_item_registry import line_item_registry

from plugins.stripe.stripe.constants import STRIPE_CURRENCY_MULTIPLIER

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
    from plugins.stripe.stripe.sdk_adapter import StripeSDKAdapter

    prefix = "test_" if config.get("sandbox", True) else "live_"
    return StripeSDKAdapter(
        SDKConfig(
            api_key=config.get(f"{prefix}secret_key") or config.get("secret_key", ""),
            sandbox=config.get("sandbox", True),
        )
    )


def _build_stripe_subscription_items(invoice):
    """Convert RECURRING invoice line items to Stripe subscription line_items.

    Which line items are recurring (and their name/period) comes from the
    extensible line-item registry — each plugin declares its own recurring
    types. One-off items (token bundles, shop items, add-ons sold once, …)
    return no spec and are charged once, not as a subscription. stripe imports
    no subscription model.
    """
    items = []
    currency = (invoice.currency or "EUR").lower()

    for li in invoice.line_items:
        spec = line_item_registry.recurring_billing_spec(li)
        if not spec:
            continue
        recurring = BILLING_PERIOD_TO_STRIPE.get(
            spec.billing_period, {"interval": "month"}
        )
        items.append(
            {
                "price_data": {
                    "currency": currency,
                    "unit_amount": int(li.unit_price * STRIPE_CURRENCY_MULTIPLIER),
                    "recurring": recurring,
                    "product_data": {"name": spec.name},
                },
                "quantity": li.quantity,
            }
        )
    return items


def _resolve_subscription_trial_days(invoice):
    """Largest positive free-trial length across the invoice's recurring items.

    Read from the extensible line-item registry (no subscription model import);
    None when no recurring item declares a trial, so cycle 1 bills immediately.
    """
    trial_days = 0
    for li in invoice.line_items:
        spec = line_item_registry.recurring_billing_spec(li)
        if spec and spec.trial_days and spec.trial_days > trial_days:
            trial_days = spec.trial_days
    return trial_days or None


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

    subscription_line_items: list = []
    if mode == "subscription":
        subscription_line_items = _build_stripe_subscription_items(invoice)
        if not subscription_line_items:
            # Divergence guard: the mode-check flagged this invoice recurring but
            # no line item produced a Stripe billing spec (e.g. a plan with no
            # billing period, whose spec lookup raised and was swallowed by the
            # registry). Never send Stripe mode=subscription with empty
            # line_items — it rejects the request. Fall back to a one-time
            # payment for the still-chargeable lines.
            logger.warning(
                "Invoice %s resolved to subscription mode but produced no Stripe "
                "subscription line items; falling back to one-time payment mode.",
                invoice.id,
            )
            mode = "payment"

    base_meta = {"invoice_id": str(invoice.id), "user_id": str(g.user_id)}
    # S21 — shared helper. Native iOS/macOS apps get a deep link they can
    # intercept via ASWebAuthenticationSession; web falls back to Origin.
    success_url, cancel_url = build_provider_redirect_urls(
        request,
        "stripe",
        success_query="?session_id={CHECKOUT_SESSION_ID}",
        ios_deep_link=True,
    )

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

        trial_period_days = _resolve_subscription_trial_days(invoice)
        response = adapter.create_subscription_session(
            customer_id=customer_id,
            line_items=subscription_line_items,
            metadata=base_meta,
            success_url=success_url,
            cancel_url=cancel_url,
            trial_period_days=trial_period_days,
        )
    else:
        # One-time: check if authorize-only or immediate capture
        from vbwd.plugins.payment_route_helpers import determine_capture_method

        capture_method = determine_capture_method(invoice)
        capture = capture_method != "manual"

        meta = {
            **base_meta,
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        response = adapter.create_payment_intent(
            amount=Decimal(str(invoice.total_amount or invoice.amount)),
            currency=(invoice.currency or "EUR"),
            metadata=meta,
            capture=capture,
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
    """Handle checkout.session.completed — emit captured or authorized event."""
    metadata = session.get("metadata", {})
    invoice_id = metadata.get("invoice_id")
    if not invoice_id:
        return

    # If subscription mode, store provider_subscription_id on our Subscription
    stripe_sub_id = session.get("subscription")
    if stripe_sub_id:
        _link_stripe_subscription(UUID(invoice_id), stripe_sub_id)

    payment_status = session.get("payment_status", "paid")
    payment_intent_id = session.get("payment_intent", "")

    if payment_status == "unpaid":
        # Authorize-only (capture_method=manual) — funds held, not charged
        from vbwd.plugins.payment_route_helpers import emit_payment_authorized

        emit_payment_authorized(
            invoice_id=UUID(invoice_id),
            payment_reference=session["id"],
            amount=str(session["amount_total"] / 100),
            currency=session.get("currency", "usd"),
            provider="stripe",
            payment_intent_id=payment_intent_id,
        )
    else:
        # Immediate capture — funds charged. Store authorised+captured amounts
        # for the fe-admin "Payment Information" block (refunded_amount is
        # written by the refund handler when applicable).
        captured_amount_str = f"{session['amount_total'] / 100:.2f}"
        emit_payment_captured(
            invoice_id=UUID(invoice_id),
            payment_reference=session["id"],
            amount=captured_amount_str,
            currency=session.get("currency", "usd"),
            provider="stripe",
            transaction_id=payment_intent_id,
            metadata={
                "stripe": {
                    "session_id": session["id"],
                    "payment_intent_id": payment_intent_id,
                    "authorised_amount": captured_amount_str,
                    "captured_amount": captured_amount_str,
                }
            },
        )


def _handle_invoice_paid(stripe_invoice):
    """Handle Stripe invoice.paid — renewal payment for subscriptions."""
    # Skip the first invoice (already handled by checkout.session.completed)
    if stripe_invoice.get("billing_reason") == "subscription_create":
        return

    stripe_sub_id = stripe_invoice.get("subscription")
    if not stripe_sub_id:
        return

    # Renewal invoice creation is owned by the recurring-object plugin (e.g.
    # subscription), which subscribes to this fact. Stripe publishes blindly —
    # no subscriber ⇒ no-op, so stripe stays subscription-free. The subscriber
    # creates the renewal invoice and re-emits payment.captured, forwarding the
    # exact metadata below so downstream capture handling is preserved.
    captured_amount_str = f"{stripe_invoice['amount_paid'] / 100:.2f}"
    publish_recurring_charge(
        provider="stripe",
        provider_ref_id=stripe_sub_id,
        amount=captured_amount_str,
        currency=stripe_invoice.get("currency", "usd"),
        provider_reference=stripe_invoice["id"],
        transaction_id=stripe_invoice.get("payment_intent", ""),
        metadata={
            "stripe": {
                "invoice_id": stripe_invoice["id"],
                "payment_intent_id": stripe_invoice.get("payment_intent", ""),
                "billing_reason": stripe_invoice.get("billing_reason", "renewal"),
                "authorised_amount": captured_amount_str,
                "captured_amount": captured_amount_str,
            }
        },
    )


def _handle_subscription_deleted(stripe_sub):
    """Handle Stripe customer.subscription.deleted — cancel our subscription."""
    publish_provider_cancelled(
        provider="stripe",
        provider_ref_id=stripe_sub["id"],
        reason="stripe_subscription_deleted",
    )


def _handle_payment_failed(stripe_invoice):
    """Handle Stripe invoice.payment_failed — renewal charge failed."""
    stripe_sub_id = stripe_invoice.get("subscription")
    if not stripe_sub_id:
        return

    error_message = (
        stripe_invoice.get("last_payment_error", {}).get("message", "Payment failed")
        if isinstance(stripe_invoice.get("last_payment_error"), dict)
        else "Payment failed"
    )
    publish_recurring_failed(
        provider="stripe",
        provider_ref_id=stripe_sub_id,
        error_message=error_message,
    )


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


def _resolve_stripe_api_key(config) -> str:
    """Pick the right Stripe API key from plugin config (sandbox vs live)."""
    prefix = "test_" if config.get("sandbox", True) else "live_"
    return config.get(f"{prefix}secret_key") or config.get("secret_key", "")


def _resolve_payment_intent_from_refund(refund_obj, api_key) -> str | None:
    """Return the payment_intent_id this refund belongs to, or None.

    Most refunds carry ``payment_intent`` directly. Older shapes require
    a charge lookup; we tolerate either.
    """
    payment_intent_id = refund_obj.get("payment_intent")
    if payment_intent_id:
        return payment_intent_id

    charge_id = refund_obj.get("charge")
    if not charge_id:
        return None

    import stripe

    stripe.api_key = api_key
    try:
        charge = stripe.Charge.retrieve(charge_id)
    except Exception:
        logger.exception("Failed to retrieve charge %s for refund reversal", charge_id)
        return None
    return (
        charge.get("payment_intent")
        if isinstance(charge, dict)
        else getattr(charge, "payment_intent", None)
    )


def _find_invoice_id_for_payment_intent(payment_intent_id, api_key) -> UUID | None:
    """Look up our invoice_id from the Stripe checkout Session, or None."""
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
        return None

    if not sessions.data:
        return None

    metadata = dict(sessions.data[0].metadata or {})
    invoice_id_str = metadata.get("invoice_id")
    if not invoice_id_str:
        return None
    try:
        return UUID(invoice_id_str)
    except (ValueError, TypeError):
        return None


def _handle_refund_updated(refund_obj, config):
    """Handle Stripe refund.updated — restore the invoice when canceled.

    S23 — broke the trace (refund → payment_intent → session → invoice_id)
    into two private helpers; this orchestration is now ~20 LOC.
    """
    if refund_obj.get("status") != "canceled":
        return

    api_key = _resolve_stripe_api_key(config)

    payment_intent_id = _resolve_payment_intent_from_refund(refund_obj, api_key)
    if not payment_intent_id:
        return

    invoice_id = _find_invoice_id_for_payment_intent(payment_intent_id, api_key)
    if not invoice_id:
        return

    current_app.container.event_dispatcher().emit(
        RefundReversedEvent(
            invoice_id=invoice_id,
            reason="stripe_refund_canceled",
            provider="stripe",
        )
    )
    logger.info(
        "Refund reversal processed for invoice %s, refund %s",
        invoice_id,
        refund_obj.get("id"),
    )


def _link_stripe_subscription(invoice_id, provider_subscription_id):
    """Publish that Stripe's recurring object is linked to this invoice.

    Stripe stays subscription-free — it publishes the fact and the
    recurring-object plugin (if any) records the id. No-op if no subscriber.
    """
    publish_provider_linked(
        invoice_id=invoice_id,
        provider="stripe",
        provider_ref_id=provider_subscription_id,
    )


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

    # Include invoice_id from session metadata so frontend can redirect to confirmation
    metadata = data.get("metadata", {})
    invoice_id = metadata.get("invoice_id")

    # Fallback: look up by provider_session_id
    if not invoice_id:
        invoice = None
        stripe_sid = data.get("session_id", "")
        if stripe_sid:
            container = current_app.container
            invoice_repo = container.invoice_repository()
            invoice = invoice_repo.find_by_provider_session_id(stripe_sid)
            if invoice:
                invoice_id = str(invoice.id)

    return (
        jsonify(
            {
                "status": data.get("status"),
                "amount_total": data.get("amount_total"),
                "currency": data.get("currency"),
                "invoice_id": invoice_id,
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

    captured_amount_str = f"{(session_data.get('amount_total') or 0) / 100:.2f}"
    emit_payment_captured(
        invoice_id=invoice_id,
        payment_reference=session_data.get("session_id", ""),
        amount=captured_amount_str,
        currency=session_data.get("currency", "usd"),
        provider="stripe",
        transaction_id=session_data.get("payment_intent", ""),
        metadata={
            "stripe": {
                "session_id": session_data.get("session_id", ""),
                "payment_intent_id": session_data.get("payment_intent", ""),
                "reconciled": True,
                "authorised_amount": captured_amount_str,
                "captured_amount": captured_amount_str,
            }
        },
    )
