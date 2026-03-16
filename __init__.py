"""Stripe payment provider plugin."""
from typing import Optional, Dict, Any, TYPE_CHECKING
from decimal import Decimal
from uuid import UUID
from src.plugins.base import BasePlugin, PluginMetadata
from src.plugins.payment_provider import PaymentProviderPlugin, PaymentResult, PaymentStatus

if TYPE_CHECKING:
    from flask import Blueprint


class StripePlugin(PaymentProviderPlugin):
    """Stripe payment provider — Checkout Sessions with webhooks.

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
            dependencies=[],
        )

    def get_blueprint(self) -> Optional["Blueprint"]:
        from plugins.stripe.routes import stripe_plugin_bp
        return stripe_plugin_bp

    def get_url_prefix(self) -> Optional[str]:
        return "/api/v1/plugins/stripe"

    def on_enable(self) -> None:
        pass  # Stateless — config read per-request from config_store

    def on_disable(self) -> None:
        pass

    def _get_adapter(self):
        """Instantiate StripeSDKAdapter from config_store (per-request)."""
        from flask import current_app
        from plugins.stripe.sdk_adapter import StripeSDKAdapter
        from src.sdk.interface import SDKConfig

        config_store = current_app.config_store
        config = config_store.get_config("stripe")
        prefix = "test_" if config.get("sandbox", True) else "live_"
        return StripeSDKAdapter(SDKConfig(
            api_key=config.get(f"{prefix}secret_key") or config.get("secret_key", ""),
            sandbox=config.get("sandbox", True),
        ))

    def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        subscription_id: UUID,
        user_id: UUID,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentResult:
        adapter = self._get_adapter()
        meta = metadata or {}
        meta.update({"subscription_id": str(subscription_id), "user_id": str(user_id)})
        resp = adapter.create_payment_intent(amount, currency, meta)
        if resp.success:
            return PaymentResult(
                success=True,
                transaction_id=resp.data.get("session_id"),
                status=PaymentStatus.PENDING,
                metadata=resp.data,
            )
        return PaymentResult(success=False, error_message=resp.error)

    def process_payment(self, payment_intent_id: str, payment_method: str) -> PaymentResult:
        adapter = self._get_adapter()
        resp = adapter.capture_payment(payment_intent_id)
        if resp.success:
            status = PaymentStatus.COMPLETED if resp.data.get("status") == "paid" else PaymentStatus.PROCESSING
            return PaymentResult(success=True, transaction_id=payment_intent_id, status=status)
        return PaymentResult(success=False, error_message=resp.error)

    def refund_payment(self, transaction_id: str, amount: Optional[Decimal] = None) -> PaymentResult:
        adapter = self._get_adapter()
        resp = adapter.refund_payment(transaction_id, amount)
        if resp.success:
            return PaymentResult(
                success=True,
                transaction_id=resp.data.get("refund_id"),
                status=PaymentStatus.REFUNDED,
            )
        return PaymentResult(success=False, error_message=resp.error)

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        from flask import current_app
        config = current_app.config_store.get_config("stripe")
        prefix = "test_" if config.get("sandbox", True) else "live_"
        webhook_secret = config.get(f"{prefix}webhook_secret") or config.get("webhook_secret", "")
        adapter = self._get_adapter()
        try:
            adapter.verify_webhook_signature(payload, signature, webhook_secret)
            return True
        except Exception:
            return False

    def handle_webhook(self, payload: Dict[str, Any]) -> None:
        pass  # Webhook handling is done in routes.py (event-driven)
