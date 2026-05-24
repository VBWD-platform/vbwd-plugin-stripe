"""Tests for recurring billing logic in Stripe plugin routes.

Covers:
- Session mode determination (payment vs subscription)
- Line item building for recurring plans
- Webhook handling for invoice.paid, subscription.deleted, payment_failed
"""
import sys
import pytest
from decimal import Decimal
from uuid import uuid4, UUID
from unittest.mock import MagicMock

from flask import Flask

from vbwd.plugins.config_store import PluginConfigEntry
from vbwd.models.enums import (
    InvoiceStatus,
    LineItemType,
    BillingPeriod,
)
from vbwd.events.line_item_registry import RecurringBillingSpec


@pytest.fixture
def mock_stripe(mocker):
    """Mock stripe module."""
    mock_mod = mocker.MagicMock()
    mock_mod.error.SignatureVerificationError = type(
        "SignatureVerificationError", (Exception,), {}
    )
    mock_mod.error.StripeError = type("StripeError", (Exception,), {})
    mocker.patch.dict(sys.modules, {"stripe": mock_mod})
    return mock_mod


@pytest.fixture
def stripe_config():
    return {
        "test_publishable_key": "pk_test",
        "test_secret_key": "sk_test",
        "test_webhook_secret": "whsec_test",
        "sandbox": True,
    }


@pytest.fixture
def mock_config_store(mocker, stripe_config):
    store = mocker.MagicMock()
    store.get_by_name.return_value = PluginConfigEntry(
        plugin_name="stripe", status="enabled", config=stripe_config
    )
    store.get_config.return_value = stripe_config
    return store


@pytest.fixture
def mock_container(mocker):
    container = mocker.MagicMock()
    container.invoice_repository.return_value = mocker.MagicMock()
    container.subscription_repository.return_value = mocker.MagicMock()
    container.user_repository.return_value = mocker.MagicMock()
    container.addon_subscription_repository.return_value = mocker.MagicMock()
    container.event_dispatcher.return_value = mocker.MagicMock()
    return container


@pytest.fixture
def app(mock_stripe, mock_config_store, mock_container, mocker):
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True

    # Mock the auth machinery that require_auth calls at request time
    user_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    mock_auth_service = MagicMock()
    mock_auth_service.return_value.verify_token.return_value = str(user_id)
    mocker.patch("vbwd.middleware.auth.AuthService", mock_auth_service)

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.status.value = "ACTIVE"

    mock_user_repo = MagicMock()
    mock_user_repo.return_value.find_by_id.return_value = mock_user
    mocker.patch("vbwd.middleware.auth.UserRepository", mock_user_repo)

    mock_auth_db = MagicMock()
    mocker.patch("vbwd.middleware.auth.db", mock_auth_db)

    from plugins.stripe.stripe.routes import stripe_plugin_bp

    flask_app.register_blueprint(stripe_plugin_bp, url_prefix="/api/v1/plugins/stripe")
    flask_app.config_store = mock_config_store
    flask_app.container = mock_container
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test_token"}


# ---- Helpers to build mock invoice + line items ----


def _make_invoice(user_id, line_items, total_amount=Decimal("9.99"), currency="EUR"):
    inv = MagicMock()
    inv.id = uuid4()
    inv.status = InvoiceStatus.PENDING
    inv.user_id = user_id
    inv.total_amount = total_amount
    inv.amount = total_amount
    inv.currency = currency
    inv.line_items = line_items
    return inv


def _make_subscription_line_item(
    billing_period, plan_name="Pro Plan", unit_price=Decimal("9.99")
):
    """Mock subscription line item the line-item registry reports as recurring.

    The ``recurring_registry`` fixture's fake handler reads ``_recurring_spec``.
    """
    li = MagicMock()
    li.item_type = LineItemType.SUBSCRIPTION
    li.item_id = uuid4()
    li.unit_price = unit_price
    li.quantity = 1
    li._recurring_spec = RecurringBillingSpec(
        name=plan_name, billing_period=billing_period.value
    )
    return li


def _make_addon_line_item(
    billing_period_str, addon_name="Extra Storage", unit_price=Decimal("4.99")
):
    """Mock recurring add-on line item (registry reports it recurring)."""
    li = MagicMock()
    li.item_type = LineItemType.ADD_ON
    li.item_id = uuid4()
    li.unit_price = unit_price
    li.quantity = 2
    li._recurring_spec = RecurringBillingSpec(
        name=addon_name, billing_period=billing_period_str
    )
    return li


def _make_one_time_line_item():
    """Mock token bundle line item (one-time — no recurring spec)."""
    li = MagicMock()
    li.item_type = LineItemType.TOKEN_BUNDLE
    li.item_id = uuid4()
    li.unit_price = Decimal("19.99")
    li.quantity = 1
    li._recurring_spec = None
    return li


# ---- Mode determination tests ----


class TestDetermineSessionMode:
    """Mode determination delegates recurrence to the line-item registry."""

    def test_determine_mode_recurring_plan(self, recurring_registry):
        """Should return 'subscription' for monthly recurring plan."""
        from vbwd.plugins.payment_route_helpers import determine_session_mode

        invoice = MagicMock()
        invoice.line_items = [_make_subscription_line_item(BillingPeriod.MONTHLY)]

        assert determine_session_mode(invoice) == "subscription"

    def test_determine_mode_one_time_plan(self, recurring_registry):
        """Should return 'payment' for one-time (token bundle) items."""
        from vbwd.plugins.payment_route_helpers import determine_session_mode

        invoice = MagicMock()
        invoice.line_items = [_make_one_time_line_item()]

        assert determine_session_mode(invoice) == "payment"

    def test_determine_mode_recurring_addon(self, recurring_registry):
        """Should return 'subscription' for recurring add-on."""
        from vbwd.plugins.payment_route_helpers import determine_session_mode

        invoice = MagicMock()
        invoice.line_items = [_make_addon_line_item("monthly")]

        assert determine_session_mode(invoice) == "subscription"

    def test_determine_mode_mixed_one_time(self, recurring_registry):
        """Should return 'payment' when all items are one-time tokens."""
        from vbwd.plugins.payment_route_helpers import determine_session_mode

        invoice = MagicMock()
        invoice.line_items = [_make_one_time_line_item(), _make_one_time_line_item()]

        assert determine_session_mode(invoice) == "payment"


# ---- Subscription line item building tests ----


class TestBuildSubscriptionItems:
    """_build_stripe_subscription_items reads the registry's billing spec."""

    def test_build_subscription_items_monthly(self, recurring_registry):
        """Monthly plan should produce interval='month'."""
        from plugins.stripe.stripe.routes import _build_stripe_subscription_items

        invoice = MagicMock()
        invoice.line_items = [_make_subscription_line_item(BillingPeriod.MONTHLY)]
        invoice.currency = "EUR"

        items = _build_stripe_subscription_items(invoice)

        assert len(items) == 1
        assert items[0]["price_data"]["recurring"]["interval"] == "month"

    def test_build_subscription_items_yearly(self, recurring_registry):
        """Yearly plan should produce interval='year'."""
        from plugins.stripe.stripe.routes import _build_stripe_subscription_items

        invoice = MagicMock()
        invoice.line_items = [_make_subscription_line_item(BillingPeriod.YEARLY)]
        invoice.currency = "EUR"

        items = _build_stripe_subscription_items(invoice)

        assert items[0]["price_data"]["recurring"]["interval"] in ("year",)

    def test_build_subscription_items_quarterly(self, recurring_registry):
        """Quarterly plan should produce interval_count=3 with interval='month'."""
        from plugins.stripe.stripe.routes import _build_stripe_subscription_items

        invoice = MagicMock()
        invoice.line_items = [_make_subscription_line_item(BillingPeriod.QUARTERLY)]
        invoice.currency = "USD"

        items = _build_stripe_subscription_items(invoice)

        recurring = items[0]["price_data"]["recurring"]
        assert recurring["interval"] == "month"
        assert recurring["interval_count"] == 3


# ---- create-session subscription mode test ----


class TestCreateSessionSubscriptionMode:
    """Test create-session uses mode=subscription for recurring invoices."""

    def test_create_session_subscription_mode(
        self, client, auth_headers, mock_container, mock_stripe, recurring_registry
    ):
        """create-session should call create_subscription_session for recurring invoice."""
        user_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        li = _make_subscription_line_item(BillingPeriod.MONTHLY)
        invoice = _make_invoice(user_id, [li])

        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice

        # Mock user with existing payment_customer_id
        user = MagicMock()
        user.email = "test@example.com"
        user.payment_customer_id = "cus_existing"
        mock_container.user_repository.return_value.find_by_id.return_value = user

        # Mock subscription session creation
        mock_session = MagicMock()
        mock_session.id = "cs_sub_test"
        mock_session.url = "https://checkout.stripe.com/cs_sub_test"
        mock_stripe.checkout.Session.create.return_value = mock_session

        resp = client.post(
            "/api/v1/plugins/stripe/create-session",
            json={"invoice_id": str(invoice.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Verify mode=subscription was used
        call_kwargs = mock_stripe.checkout.Session.create.call_args
        assert (
            call_kwargs.kwargs.get("mode") == "subscription"
            or call_kwargs[1].get("mode") == "subscription"
        )

    def test_create_session_reuses_customer(
        self, client, auth_headers, mock_container, mock_stripe, recurring_registry
    ):
        """Should reuse existing payment_customer_id without creating a new customer."""
        user_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        li = _make_subscription_line_item(BillingPeriod.MONTHLY)
        invoice = _make_invoice(user_id, [li])

        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice

        user = MagicMock()
        user.email = "test@example.com"
        user.payment_customer_id = "cus_existing_123"
        mock_container.user_repository.return_value.find_by_id.return_value = user

        mock_session = MagicMock()
        mock_session.id = "cs_sub"
        mock_session.url = "https://checkout.stripe.com/cs_sub"
        mock_stripe.checkout.Session.create.return_value = mock_session

        client.post(
            "/api/v1/plugins/stripe/create-session",
            json={"invoice_id": str(invoice.id)},
            headers=auth_headers,
        )

        # Customer.create should NOT be called since customer already has payment_customer_id
        mock_stripe.Customer.create.assert_not_called()

        # Session.create should use existing customer_id
        call_kwargs = mock_stripe.checkout.Session.create.call_args
        customer_arg = call_kwargs.kwargs.get("customer") or call_kwargs[1].get(
            "customer"
        )
        assert customer_arg == "cus_existing_123"


# ---- Webhook recurring billing tests ----


class TestWebhookRecurring:
    """Tests for recurring-specific webhook events."""

    def _post_webhook(self, client, mock_stripe, event_type, obj):
        mock_stripe.Webhook.construct_event.return_value = {
            "type": event_type,
            "data": {"object": obj},
        }
        return client.post(
            "/api/v1/plugins/stripe/webhook",
            data=b"body",
            headers={"Stripe-Signature": "sig"},
            content_type="application/json",
        )

    def test_webhook_checkout_stores_subscription_id(
        self, client, mock_stripe, mock_container, fake_lifecycle
    ):
        """checkout.session.completed with a subscription links it via the port."""
        invoice_id = uuid4()

        self._post_webhook(
            client,
            mock_stripe,
            "checkout.session.completed",
            {
                "id": "cs_test",
                "metadata": {"invoice_id": str(invoice_id), "user_id": str(uuid4())},
                "amount_total": 999,
                "currency": "eur",
                "payment_intent": "pi_test",
                "subscription": "sub_stripe_123",
            },
        )

        fake_lifecycle.link_provider_subscription.assert_called_once_with(
            invoice_id, "sub_stripe_123"
        )

    def test_webhook_invoice_paid_creates_renewal(
        self, client, mock_stripe, mock_container, fake_lifecycle
    ):
        """invoice.paid (renewal) records the renewal via the port and emits."""
        renewal_invoice_id = uuid4()
        fake_lifecycle.record_provider_renewal.return_value = renewal_invoice_id

        self._post_webhook(
            client,
            mock_stripe,
            "invoice.paid",
            {
                "id": "in_stripe_renewal",
                "subscription": "sub_stripe_123",
                "billing_reason": "subscription_cycle",
                "amount_paid": 999,
                "currency": "eur",
                "payment_intent": "pi_renewal",
            },
        )

        fake_lifecycle.record_provider_renewal.assert_called_once_with(
            provider="stripe",
            provider_subscription_id="sub_stripe_123",
            amount="9.99",
            currency="eur",
            provider_reference="in_stripe_renewal",
        )
        dispatcher = mock_container.event_dispatcher.return_value
        dispatcher.emit.assert_called_once()
        assert dispatcher.emit.call_args[0][0].invoice_id == renewal_invoice_id

    def test_webhook_invoice_paid_skips_first(
        self, client, mock_stripe, mock_container, fake_lifecycle
    ):
        """invoice.paid with billing_reason=subscription_create should be skipped."""
        self._post_webhook(
            client,
            mock_stripe,
            "invoice.paid",
            {
                "id": "in_first",
                "subscription": "sub_123",
                "billing_reason": "subscription_create",
                "amount_paid": 999,
                "currency": "eur",
                "payment_intent": "pi_first",
            },
        )

        fake_lifecycle.record_provider_renewal.assert_not_called()
        mock_container.event_dispatcher.return_value.emit.assert_not_called()

    def test_webhook_invoice_paid_no_renewal_no_emit(
        self, client, mock_stripe, mock_container, fake_lifecycle
    ):
        """When the port returns no renewal id (no match / already processed),
        the webhook emits nothing — dedup is the port's concern."""
        fake_lifecycle.record_provider_renewal.return_value = None

        self._post_webhook(
            client,
            mock_stripe,
            "invoice.paid",
            {
                "id": "in_duplicate",
                "subscription": "sub_123",
                "billing_reason": "subscription_cycle",
                "amount_paid": 999,
                "currency": "eur",
                "payment_intent": "pi_dup",
            },
        )

        fake_lifecycle.record_provider_renewal.assert_called_once()
        mock_container.event_dispatcher.return_value.emit.assert_not_called()

    def test_webhook_subscription_deleted_cancels(
        self, client, mock_stripe, mock_container, fake_lifecycle
    ):
        """customer.subscription.deleted cancels via the lifecycle port."""
        self._post_webhook(
            client,
            mock_stripe,
            "customer.subscription.deleted",
            {"id": "sub_deleted_123"},
        )

        fake_lifecycle.cancel_by_provider_subscription_id.assert_called_once_with(
            provider="stripe",
            provider_subscription_id="sub_deleted_123",
            reason="stripe_subscription_deleted",
        )

    def test_webhook_payment_failed_emits_event(
        self, client, mock_stripe, mock_container, fake_lifecycle
    ):
        """invoice.payment_failed flags failure via the lifecycle port."""
        self._post_webhook(
            client,
            mock_stripe,
            "invoice.payment_failed",
            {
                "id": "in_failed",
                "subscription": "sub_123",
                "last_payment_error": {"message": "Card was declined"},
                "payment_intent": "pi_failed",
            },
        )

        fake_lifecycle.mark_provider_payment_failed.assert_called_once_with(
            provider="stripe",
            provider_subscription_id="sub_123",
            error_message="Card was declined",
        )


class TestBillingPeriodToStripeDaily:
    """Tests that DAILY billing period maps to interval=day in Stripe."""

    def test_daily_period_maps_to_day_interval(self):
        from plugins.stripe.stripe.routes import BILLING_PERIOD_TO_STRIPE

        assert "DAILY" in BILLING_PERIOD_TO_STRIPE
        assert BILLING_PERIOD_TO_STRIPE["DAILY"]["interval"] == "day"

    def test_build_subscription_items_daily(self, recurring_registry):
        from plugins.stripe.stripe.routes import _build_stripe_subscription_items
        from vbwd.models.enums import BillingPeriod

        invoice = MagicMock()
        invoice.line_items = [_make_subscription_line_item(BillingPeriod.DAILY)]
        invoice.currency = "EUR"

        items = _build_stripe_subscription_items(invoice)

        assert len(items) == 1
        assert items[0]["price_data"]["recurring"]["interval"] == "day"
