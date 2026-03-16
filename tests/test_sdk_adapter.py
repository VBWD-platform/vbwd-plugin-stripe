"""Tests for StripeSDKAdapter."""
import sys
import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from src.sdk.interface import SDKConfig, SDKResponse
from src.sdk.base import BaseSDKAdapter


@pytest.fixture
def mock_stripe(mocker):
    """Mock the stripe module before StripeSDKAdapter is imported."""
    mock_mod = mocker.MagicMock()
    mocker.patch.dict(sys.modules, {"stripe": mock_mod})
    return mock_mod


@pytest.fixture
def adapter(mock_stripe):
    """Create StripeSDKAdapter with mocked stripe module."""
    from plugins.stripe.sdk_adapter import StripeSDKAdapter

    config = SDKConfig(api_key="sk_test_123", sandbox=True)
    return StripeSDKAdapter(config)


class TestStripeSDKAdapter:
    """Tests for StripeSDKAdapter."""

    def test_provider_name(self, adapter):
        """provider_name should return 'stripe'."""
        assert adapter.provider_name == "stripe"

    def test_inherits_base_sdk_adapter(self, adapter):
        """StripeSDKAdapter should inherit from BaseSDKAdapter."""
        assert isinstance(adapter, BaseSDKAdapter)

    def test_create_payment_intent_success(self, adapter, mock_stripe):
        """create_payment_intent should return SDKResponse with session data."""
        mock_session = MagicMock()
        mock_session.id = "cs_test_session_id"
        mock_session.url = "https://checkout.stripe.com/pay/cs_test_session_id"
        mock_stripe.checkout.Session.create.return_value = mock_session

        result = adapter.create_payment_intent(
            amount=Decimal("29.99"),
            currency="EUR",
            metadata={
                "invoice_id": "inv_123",
                "success_url": "https://example.com/ok",
                "cancel_url": "https://example.com/cancel",
            },
        )

        assert result.success is True
        assert result.data["session_id"] == "cs_test_session_id"
        assert result.data["session_url"] == "https://checkout.stripe.com/pay/cs_test_session_id"

    def test_create_payment_intent_stripe_error(self, adapter, mock_stripe):
        """create_payment_intent should return error on StripeError."""
        mock_stripe.error.StripeError = type("StripeError", (Exception,), {})
        mock_stripe.checkout.Session.create.side_effect = mock_stripe.error.StripeError(
            "Card declined"
        )

        result = adapter.create_payment_intent(
            amount=Decimal("10.00"),
            currency="USD",
            metadata={"success_url": "", "cancel_url": ""},
        )

        assert result.success is False
        assert "Card declined" in result.error

    def test_create_payment_intent_amount_cents(self, adapter, mock_stripe):
        """unit_amount should be 2999 for Decimal('29.99')."""
        mock_session = MagicMock()
        mock_session.id = "cs_test"
        mock_session.url = "https://checkout.stripe.com/cs_test"
        mock_stripe.checkout.Session.create.return_value = mock_session

        adapter.create_payment_intent(
            amount=Decimal("29.99"),
            currency="EUR",
            metadata={"success_url": "", "cancel_url": ""},
        )

        call_kwargs = mock_stripe.checkout.Session.create.call_args
        line_items = call_kwargs.kwargs.get("line_items") or call_kwargs[1].get("line_items")
        unit_amount = line_items[0]["price_data"]["unit_amount"]
        assert unit_amount == 2999

    def test_create_payment_intent_metadata(self, adapter, mock_stripe):
        """Metadata fields should be forwarded to Stripe Session."""
        mock_session = MagicMock()
        mock_session.id = "cs_test"
        mock_session.url = "https://checkout.stripe.com/cs_test"
        mock_stripe.checkout.Session.create.return_value = mock_session

        adapter.create_payment_intent(
            amount=Decimal("10.00"),
            currency="USD",
            metadata={
                "invoice_id": "inv_abc",
                "user_id": "u_456",
                "success_url": "",
                "cancel_url": "",
            },
        )

        call_kwargs = mock_stripe.checkout.Session.create.call_args
        passed_meta = call_kwargs.kwargs.get("metadata") or call_kwargs[1].get("metadata")
        assert passed_meta["invoice_id"] == "inv_abc"
        assert passed_meta["user_id"] == "u_456"

    def test_capture_payment(self, adapter, mock_stripe):
        """capture_payment should retrieve session and return status."""
        mock_session = MagicMock()
        mock_session.payment_status = "paid"
        mock_session.id = "cs_test_cap"
        mock_stripe.checkout.Session.retrieve.return_value = mock_session

        result = adapter.capture_payment("cs_test_cap")

        assert result.success is True
        assert result.data["status"] == "paid"
        assert result.data["session_id"] == "cs_test_cap"

    def test_refund_full(self, adapter, mock_stripe):
        """refund_payment with no amount should issue full refund."""
        mock_session = MagicMock()
        mock_session.payment_intent = "pi_original"
        mock_stripe.checkout.Session.retrieve.return_value = mock_session

        mock_refund = MagicMock()
        mock_refund.id = "re_full"
        mock_refund.status = "succeeded"
        mock_stripe.Refund.create.return_value = mock_refund

        result = adapter.refund_payment("cs_test_refund")

        assert result.success is True
        assert result.data["refund_id"] == "re_full"
        # Full refund: no "amount" in params
        call_kwargs = mock_stripe.Refund.create.call_args
        assert "amount" not in (call_kwargs.kwargs or {})

    def test_refund_partial(self, adapter, mock_stripe):
        """refund_payment with amount should pass amount in cents."""
        mock_session = MagicMock()
        mock_session.payment_intent = "pi_original"
        mock_stripe.checkout.Session.retrieve.return_value = mock_session

        mock_refund = MagicMock()
        mock_refund.id = "re_partial"
        mock_refund.status = "succeeded"
        mock_stripe.Refund.create.return_value = mock_refund

        result = adapter.refund_payment("cs_test_refund", amount=Decimal("15.00"))

        assert result.success is True
        call_kwargs = mock_stripe.Refund.create.call_args
        passed_amount = call_kwargs.kwargs.get("amount") or call_kwargs[1].get("amount")
        assert passed_amount == 1500

    def test_get_payment_status(self, adapter, mock_stripe):
        """get_payment_status should return status, amount_total, currency."""
        mock_session = MagicMock()
        mock_session.payment_status = "paid"
        mock_session.amount_total = 4999
        mock_session.currency = "eur"
        mock_stripe.checkout.Session.retrieve.return_value = mock_session

        result = adapter.get_payment_status("cs_test_status")

        assert result.success is True
        assert result.data["status"] == "paid"
        assert result.data["amount_total"] == 4999
        assert result.data["currency"] == "eur"

    def test_webhook_signature_valid(self, adapter, mock_stripe):
        """verify_webhook_signature should return event when signature valid."""
        expected_event = {"type": "checkout.session.completed", "data": {}}
        mock_stripe.Webhook.construct_event.return_value = expected_event

        result = adapter.verify_webhook_signature(b"payload", "sig_header", "whsec_test")

        assert result == expected_event
        mock_stripe.Webhook.construct_event.assert_called_once_with(
            b"payload", "sig_header", "whsec_test"
        )

    def test_webhook_signature_invalid(self, adapter, mock_stripe):
        """verify_webhook_signature should raise on invalid signature."""
        mock_stripe.Webhook.construct_event.side_effect = ValueError("Invalid signature")

        with pytest.raises(ValueError, match="Invalid signature"):
            adapter.verify_webhook_signature(b"payload", "bad_sig", "whsec_test")

    def test_create_subscription_session(self, adapter, mock_stripe):
        """create_subscription_session should use mode=subscription."""
        mock_session = MagicMock()
        mock_session.id = "cs_sub_123"
        mock_session.url = "https://checkout.stripe.com/cs_sub_123"
        mock_stripe.checkout.Session.create.return_value = mock_session

        result = adapter.create_subscription_session(
            customer_id="cus_test",
            line_items=[{"price_data": {"currency": "eur", "unit_amount": 999, "recurring": {"interval": "month"}}}],
            metadata={"invoice_id": "inv_1"},
            success_url="https://example.com/ok",
            cancel_url="https://example.com/cancel",
        )

        assert result.success is True
        assert result.data["session_id"] == "cs_sub_123"
        call_kwargs = mock_stripe.checkout.Session.create.call_args
        assert call_kwargs.kwargs.get("mode") == "subscription" or call_kwargs[1].get("mode") == "subscription"
