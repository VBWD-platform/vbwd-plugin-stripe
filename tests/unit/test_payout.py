"""Payout capability specs (S79 Slice 2) — Stripe Connect Transfers.

RED-first oracle for the `PayoutProvider` contract on Stripe:
- `StripeSDKAdapter.create_transfer` sends the smallest-currency-unit
  amount (via STRIPE_CURRENCY_MULTIPLIER), the `acct_…` destination and
  the reference id as the idempotency key.
- `StripePlugin.create_payout` maps adapter responses to `PayoutResult`
  and every failure to the typed `PayoutError`.
"""
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from vbwd.plugins.payment_provider import PayoutError, PayoutProvider, PayoutResult
from vbwd.sdk.interface import SDKConfig, SDKResponse


REFERENCE_ID = "withdraw-req-456"
DESTINATION_ACCOUNT = "acct_1DemoConnected"


@pytest.fixture
def mock_stripe(mocker):
    """Mock the stripe module before StripeSDKAdapter is imported."""
    mock_mod = mocker.MagicMock()
    mock_mod.error.StripeError = type("StripeError", (Exception,), {})
    mocker.patch.dict(sys.modules, {"stripe": mock_mod})
    return mock_mod


@pytest.fixture
def adapter(mock_stripe):
    from plugins.stripe.stripe.sdk_adapter import StripeSDKAdapter

    config = SDKConfig(api_key="sk_test_123", sandbox=True)
    return StripeSDKAdapter(config)


class TestCreateTransfer:
    def test_sends_connect_transfer_shape(self, adapter, mock_stripe):
        mock_transfer = MagicMock()
        mock_transfer.id = "tr_9001"
        mock_transfer.reversed = False
        mock_stripe.Transfer.create.return_value = mock_transfer

        response = adapter.create_transfer(
            amount=Decimal("12.34"),
            currency="EUR",
            destination_account=DESTINATION_ACCOUNT,
            reference_id=REFERENCE_ID,
        )

        assert response.success is True
        assert response.data["transfer_id"] == "tr_9001"
        mock_stripe.Transfer.create.assert_called_once_with(
            amount=1234,  # Decimal('12.34') × STRIPE_CURRENCY_MULTIPLIER
            currency="eur",
            destination=DESTINATION_ACCOUNT,
            transfer_group=REFERENCE_ID,
            idempotency_key=REFERENCE_ID,
        )

    def test_stripe_error_maps_to_failed_sdk_response(self, adapter, mock_stripe):
        mock_stripe.Transfer.create.side_effect = mock_stripe.error.StripeError(
            "No such destination account"
        )

        response = adapter.create_transfer(
            amount=Decimal("1.00"),
            currency="EUR",
            destination_account=DESTINATION_ACCOUNT,
            reference_id=REFERENCE_ID,
        )

        assert response.success is False
        assert "No such destination account" in response.error


class TestGetTransferStatus:
    def test_retrieves_transfer(self, adapter, mock_stripe):
        mock_transfer = MagicMock()
        mock_transfer.id = "tr_9001"
        mock_transfer.reversed = False
        mock_stripe.Transfer.retrieve.return_value = mock_transfer

        response = adapter.get_transfer_status("tr_9001")

        assert response.success is True
        assert response.data == {"transfer_id": "tr_9001", "reversed": False}
        mock_stripe.Transfer.retrieve.assert_called_once_with("tr_9001")

    def test_stripe_error_maps_to_failed_sdk_response(self, adapter, mock_stripe):
        mock_stripe.Transfer.retrieve.side_effect = mock_stripe.error.StripeError(
            "No such transfer"
        )

        response = adapter.get_transfer_status("tr_missing")

        assert response.success is False
        assert "No such transfer" in response.error


class TestStripePluginPayout:
    def _plugin_with_adapter(self, adapter_mock):
        from plugins.stripe import StripePlugin

        plugin = StripePlugin()
        plugin._get_adapter = lambda: adapter_mock
        return plugin

    def test_plugin_is_a_payout_provider(self):
        from plugins.stripe import StripePlugin

        assert issubclass(StripePlugin, PayoutProvider)

    def test_destination_schema(self):
        from plugins.stripe import StripePlugin

        assert StripePlugin().get_payout_destination_schema() == [
            {
                "name": "account_id",
                "type": "text",
                "label_key": "withdraw.destination.stripe_account_id",
            }
        ]

    def test_create_payout_returns_completed_result(self):
        adapter_mock = MagicMock()
        adapter_mock.create_transfer.return_value = SDKResponse(
            success=True, data={"transfer_id": "tr_9001", "reversed": False}
        )
        plugin = self._plugin_with_adapter(adapter_mock)

        result = plugin.create_payout(
            amount=Decimal("12.34"),
            currency="EUR",
            destination={"account_id": DESTINATION_ACCOUNT},
            reference_id=REFERENCE_ID,
        )

        assert isinstance(result, PayoutResult)
        assert result.provider_payout_id == "tr_9001"
        # A created Stripe Transfer is settled to the connected account.
        assert result.status == "completed"
        adapter_mock.create_transfer.assert_called_once_with(
            amount=Decimal("12.34"),
            currency="EUR",
            destination_account=DESTINATION_ACCOUNT,
            reference_id=REFERENCE_ID,
        )

    def test_create_payout_provider_failure_raises_payout_error(self):
        adapter_mock = MagicMock()
        adapter_mock.create_transfer.return_value = SDKResponse(
            success=False, error="No such destination account"
        )
        plugin = self._plugin_with_adapter(adapter_mock)

        with pytest.raises(PayoutError):
            plugin.create_payout(
                amount=Decimal("12.34"),
                currency="EUR",
                destination={"account_id": DESTINATION_ACCOUNT},
                reference_id=REFERENCE_ID,
            )

    def test_create_payout_without_account_id_raises_payout_error(self):
        plugin = self._plugin_with_adapter(MagicMock())

        with pytest.raises(PayoutError):
            plugin.create_payout(
                amount=Decimal("12.34"),
                currency="EUR",
                destination={},
                reference_id=REFERENCE_ID,
            )

    @pytest.mark.parametrize(
        "reversed_flag,expected",
        [(False, "completed"), (True, "failed")],
    )
    def test_get_payout_status_maps_reversed_flag(self, reversed_flag, expected):
        adapter_mock = MagicMock()
        adapter_mock.get_transfer_status.return_value = SDKResponse(
            success=True, data={"transfer_id": "tr_9001", "reversed": reversed_flag}
        )
        plugin = self._plugin_with_adapter(adapter_mock)

        assert plugin.get_payout_status("tr_9001") == expected

    def test_get_payout_status_failure_raises_payout_error(self):
        adapter_mock = MagicMock()
        adapter_mock.get_transfer_status.return_value = SDKResponse(
            success=False, error="No such transfer"
        )
        plugin = self._plugin_with_adapter(adapter_mock)

        with pytest.raises(PayoutError):
            plugin.get_payout_status("tr_missing")
