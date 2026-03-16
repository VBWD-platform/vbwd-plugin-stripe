"""Stripe SDK adapter implementing ISDKAdapter."""
from decimal import Decimal
from typing import Dict, Any, Optional

from src.sdk.base import BaseSDKAdapter
from src.sdk.interface import SDKConfig, SDKResponse


class StripeSDKAdapter(BaseSDKAdapter):
    """Stripe SDK adapter implementing ISDKAdapter.

    Uses Stripe Checkout Sessions (redirect-based, PCI-compliant).
    Liskov Substitution: interchangeable with any ISDKAdapter.
    """

    def __init__(self, config: SDKConfig, idempotency_service=None):
        super().__init__(config, idempotency_service)
        import stripe
        stripe.api_key = config.api_key
        self._stripe = stripe

    @property
    def provider_name(self) -> str:
        return "stripe"

    def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        metadata: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> SDKResponse:
        """Create a one-time Stripe Checkout Session (mode=payment)."""
        def _create():
            try:
                unit_amount = int(amount * 100)
                success_url = metadata.pop("success_url", "")
                cancel_url = metadata.pop("cancel_url", "")
                session = self._stripe.checkout.Session.create(
                    mode="payment",
                    line_items=[{
                        "price_data": {
                            "currency": currency.lower(),
                            "unit_amount": unit_amount,
                            "product_data": {"name": "Payment"},
                        },
                        "quantity": 1,
                    }],
                    metadata=metadata,
                    success_url=success_url,
                    cancel_url=cancel_url,
                )
                return SDKResponse(
                    success=True,
                    data={"session_id": session.id, "session_url": session.url},
                )
            except self._stripe.error.StripeError as e:
                return SDKResponse(
                    success=False,
                    error=str(e),
                    error_code=getattr(e, "code", None),
                )
        return self._with_idempotency(idempotency_key, _create)

    def create_subscription_session(
        self,
        customer_id: str,
        line_items: list,
        metadata: dict,
        success_url: str,
        cancel_url: str,
    ) -> SDKResponse:
        """Create a Stripe Checkout Session with mode=subscription."""
        try:
            session = self._stripe.checkout.Session.create(
                mode="subscription",
                customer=customer_id,
                line_items=line_items,
                metadata=metadata,
                success_url=success_url,
                cancel_url=cancel_url,
            )
            return SDKResponse(
                success=True,
                data={"session_id": session.id, "session_url": session.url},
            )
        except self._stripe.error.StripeError as e:
            return SDKResponse(
                success=False,
                error=str(e),
                error_code=getattr(e, "code", None),
            )

    def create_or_get_customer(self, email: str, name: str = None, metadata: dict = None) -> SDKResponse:
        """Create a Stripe Customer."""
        try:
            params = {"email": email}
            if name:
                params["name"] = name
            if metadata:
                params["metadata"] = metadata
            customer = self._stripe.Customer.create(**params)
            return SDKResponse(success=True, data={"customer_id": customer.id})
        except self._stripe.error.StripeError as e:
            return SDKResponse(success=False, error=str(e), error_code=getattr(e, "code", None))

    def cancel_subscription(self, stripe_subscription_id: str) -> SDKResponse:
        """Cancel a Stripe subscription."""
        try:
            self._stripe.Subscription.cancel(stripe_subscription_id)
            return SDKResponse(success=True, data={"status": "canceled"})
        except self._stripe.error.StripeError as e:
            return SDKResponse(success=False, error=str(e), error_code=getattr(e, "code", None))

    def capture_payment(self, payment_intent_id: str, idempotency_key: Optional[str] = None) -> SDKResponse:
        """Retrieve session status (capture is implicit with Checkout Sessions)."""
        def _capture():
            try:
                session = self._stripe.checkout.Session.retrieve(payment_intent_id)
                return SDKResponse(
                    success=True,
                    data={"status": session.payment_status, "session_id": session.id},
                )
            except self._stripe.error.StripeError as e:
                return SDKResponse(success=False, error=str(e), error_code=getattr(e, "code", None))
        return self._with_idempotency(idempotency_key, _capture)

    def refund_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        idempotency_key: Optional[str] = None,
    ) -> SDKResponse:
        """Refund a payment via the session's payment_intent."""
        def _refund():
            try:
                session = self._stripe.checkout.Session.retrieve(payment_intent_id)
                pi_id = session.payment_intent
                params = {"payment_intent": pi_id}
                if amount is not None:
                    params["amount"] = int(amount * 100)
                refund = self._stripe.Refund.create(**params)
                return SDKResponse(
                    success=True,
                    data={"refund_id": refund.id, "status": refund.status},
                )
            except self._stripe.error.StripeError as e:
                return SDKResponse(success=False, error=str(e), error_code=getattr(e, "code", None))
        return self._with_idempotency(idempotency_key, _refund)

    def get_payment_status(self, payment_intent_id: str) -> SDKResponse:
        """Get current payment/session status."""
        try:
            session = self._stripe.checkout.Session.retrieve(payment_intent_id)
            return SDKResponse(
                success=True,
                data={
                    "status": session.payment_status,
                    "amount_total": session.amount_total,
                    "currency": session.currency,
                    "metadata": dict(session.metadata or {}),
                    "session_id": session.id,
                    "payment_intent": getattr(session, "payment_intent", ""),
                    "subscription": getattr(session, "subscription", None),
                },
            )
        except self._stripe.error.StripeError as e:
            return SDKResponse(success=False, error=str(e), error_code=getattr(e, "code", None))

    def verify_webhook_signature(self, payload: bytes, signature: str, webhook_secret: str):
        """Verify Stripe webhook signature and return parsed event."""
        return self._stripe.Webhook.construct_event(payload, signature, webhook_secret)
