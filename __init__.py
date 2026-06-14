"""Stripe payment provider plugin."""
from typing import Optional, Dict, Any, List, TYPE_CHECKING
from decimal import Decimal
from uuid import UUID
from vbwd.plugins.base import PluginMetadata
from vbwd.plugins.payment_provider import (
    PaymentProviderPlugin,
    PaymentResult,
    PaymentStatus,
    PayoutError,
    PayoutProvider,
    PayoutResult,
)

if TYPE_CHECKING:
    from flask import Blueprint


PAYOUT_DESTINATION_SCHEMA: List[Dict[str, Any]] = [
    {
        "name": "account_id",
        "type": "text",
        "label_key": "withdraw.destination.stripe_account_id",
    }
]


class StripePlugin(PaymentProviderPlugin, PayoutProvider):
    """Stripe payment provider — Checkout Sessions with webhooks; payout
    via Connect Transfers to an `acct_…` connected account (S79
    `PayoutProvider` capability).

    Class MUST be defined in __init__.py (not re-exported) due to
    discovery check obj.__module__ != full_module in manager.py.
    """

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="stripe",
            version="1.0.0",
            author="VBWD Team",
            description="Stripe payment provider — Checkout Sessions with webhooks",
            # S50.4 — recurring billing is event-driven: webhooks publish
            # domain-neutral facts (payment.recurring_charge / provider_linked /
            # provider_cancelled / recurring_failed) to the bus. No subscriber
            # (subscription disabled) ⇒ the published fact is a no-op, so stripe
            # stays subscription-free and declares no subscription dependency.
            dependencies=[],
        )

    def get_blueprint(self) -> Optional["Blueprint"]:
        from plugins.stripe.stripe.routes import stripe_plugin_bp

        return stripe_plugin_bp

    def get_url_prefix(self) -> Optional[str]:
        return "/api/v1/plugins/stripe"

    @property
    def admin_permissions(self):
        return [
            {
                "key": "payments.configure",
                "label": "Payment provider settings",
                "group": "Payments",
            },
        ]

    def on_enable(self) -> None:
        pass  # Stateless — config read per-request from config_store

    def on_disable(self) -> None:
        pass

    def _get_adapter(self):
        """Instantiate StripeSDKAdapter from config_store (per-request)."""
        from flask import current_app
        from plugins.stripe.stripe.sdk_adapter import StripeSDKAdapter
        from vbwd.sdk.interface import SDKConfig

        config_store = current_app.config_store
        config = config_store.get_config("stripe")
        prefix = "test_" if config.get("sandbox", True) else "live_"
        return StripeSDKAdapter(
            SDKConfig(
                api_key=config.get(f"{prefix}secret_key")
                or config.get("secret_key", ""),
                sandbox=config.get("sandbox", True),
            )
        )

    def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        subscription_id: UUID,
        user_id: UUID,
        metadata: Optional[Dict[str, Any]] = None,
        capture: bool = True,
    ) -> PaymentResult:
        adapter = self._get_adapter()
        meta = metadata or {}
        meta.update({"subscription_id": str(subscription_id), "user_id": str(user_id)})
        resp = adapter.create_payment_intent(amount, currency, meta, capture=capture)
        status = PaymentStatus.PENDING if capture else PaymentStatus.AUTHORIZED
        if resp.success:
            return PaymentResult(
                success=True,
                transaction_id=resp.data.get("session_id"),
                status=status,
                metadata=resp.data,
            )
        return PaymentResult(success=False, error_message=resp.error)

    def capture_payment(
        self,
        payment_id: str,
        amount: Optional[Decimal] = None,
    ) -> PaymentResult:
        adapter = self._get_adapter()
        resp = adapter.capture_payment(payment_id, amount)
        if resp.success:
            return PaymentResult(
                success=True,
                transaction_id=payment_id,
                status=PaymentStatus.COMPLETED,
            )
        return PaymentResult(success=False, error_message=resp.error)

    def release_authorization(
        self,
        payment_id: str,
    ) -> PaymentResult:
        adapter = self._get_adapter()
        resp = adapter.release_authorization(payment_id)
        if resp.success:
            return PaymentResult(
                success=True,
                transaction_id=payment_id,
                status=PaymentStatus.CANCELLED,
            )
        return PaymentResult(success=False, error_message=resp.error)

    def process_payment(
        self, payment_intent_id: str, payment_method: str
    ) -> PaymentResult:
        adapter = self._get_adapter()
        resp = adapter.capture_payment(payment_intent_id)
        if resp.success:
            status = (
                PaymentStatus.COMPLETED
                if resp.data.get("status") == "paid"
                else PaymentStatus.PROCESSING
            )
            return PaymentResult(
                success=True, transaction_id=payment_intent_id, status=status
            )
        return PaymentResult(success=False, error_message=resp.error)

    def refund_payment(
        self, transaction_id: str, amount: Optional[Decimal] = None
    ) -> PaymentResult:
        adapter = self._get_adapter()
        resp = adapter.refund_payment(transaction_id, amount)
        if resp.success:
            return PaymentResult(
                success=True,
                transaction_id=resp.data.get("refund_id"),
                status=PaymentStatus.REFUNDED,
            )
        return PaymentResult(success=False, error_message=resp.error)

    def get_payout_destination_schema(self) -> List[Dict[str, Any]]:
        return PAYOUT_DESTINATION_SCHEMA

    def create_payout(
        self,
        amount: Decimal,
        currency: str,
        destination: Dict[str, Any],
        reference_id: str,
    ) -> PayoutResult:
        destination_account = str(destination.get("account_id", "")).strip()
        if not destination_account:
            raise PayoutError("Stripe payout requires a destination account_id")
        adapter = self._get_adapter()
        resp = adapter.create_transfer(
            amount=amount,
            currency=currency,
            destination_account=destination_account,
            reference_id=reference_id,
        )
        if not resp.success:
            raise PayoutError(f"Stripe payout failed: {resp.error}")
        # A created Connect Transfer is settled to the connected account
        # synchronously — acceptance means the money has moved.
        return PayoutResult(
            provider_payout_id=resp.data.get("transfer_id", ""),
            status="completed",
        )

    def get_payout_status(self, provider_payout_id: str) -> str:
        adapter = self._get_adapter()
        resp = adapter.get_transfer_status(provider_payout_id)
        if not resp.success:
            raise PayoutError(f"Stripe payout status lookup failed: {resp.error}")
        return "failed" if resp.data.get("reversed") else "completed"

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        from flask import current_app

        config = current_app.config_store.get_config("stripe")
        prefix = "test_" if config.get("sandbox", True) else "live_"
        webhook_secret = config.get(f"{prefix}webhook_secret") or config.get(
            "webhook_secret", ""
        )
        adapter = self._get_adapter()
        try:
            adapter.verify_webhook_signature(payload, signature, webhook_secret)
            return True
        except Exception:
            return False

    def handle_webhook(self, payload: Dict[str, Any]) -> None:
        pass  # Webhook handling is done in routes.py (event-driven)
