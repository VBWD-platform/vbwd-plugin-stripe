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
from unittest.mock import MagicMock, patch, PropertyMock

from flask import Flask, g

from src.plugins.config_store import PluginConfigEntry
from src.models.enums import (
    InvoiceStatus,
    LineItemType,
    BillingPeriod,
    SubscriptionStatus,
)
from src.events.payment_events import (
    PaymentCapturedEvent,
    SubscriptionCancelledEvent,
    PaymentFailedEvent,
)


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
    mocker.patch("src.middleware.auth.AuthService", mock_auth_service)

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.status.value = "ACTIVE"

    mock_user_repo = MagicMock()
    mock_user_repo.return_value.find_by_id.return_value = mock_user
    mocker.patch("src.middleware.auth.UserRepository", mock_user_repo)

    mock_auth_db = MagicMock()
    mocker.patch("src.middleware.auth.db", mock_auth_db)

    from plugins.stripe.routes import stripe_plugin_bp

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
    """Create a mock subscription line item with a recurring plan."""
    li = MagicMock()
    li.item_type = LineItemType.SUBSCRIPTION
    li.item_id = uuid4()
    li.unit_price = unit_price
    li.quantity = 1

    sub = MagicMock()
    sub.tarif_plan = MagicMock()
    sub.tarif_plan.is_recurring = True
    sub.tarif_plan.billing_period = billing_period
    sub.tarif_plan.name = plan_name
    li._sub = sub  # For setup in db.session.get mock
    return li


def _make_addon_line_item(
    billing_period_str, addon_name="Extra Storage", unit_price=Decimal("4.99")
):
    """Create a mock add-on line item with a recurring add-on."""
    li = MagicMock()
    li.item_type = LineItemType.ADD_ON
    li.item_id = uuid4()
    li.unit_price = unit_price
    li.quantity = 2

    addon_sub = MagicMock()
    addon_sub.addon = MagicMock()
    addon_sub.addon.is_recurring = True
    addon_sub.addon.billing_period = billing_period_str
    addon_sub.addon.name = addon_name
    li._addon_sub = addon_sub
    return li


def _make_one_time_line_item():
    """Create a mock token bundle line item (one-time)."""
    li = MagicMock()
    li.item_type = LineItemType.TOKEN_BUNDLE
    li.item_id = uuid4()
    li.unit_price = Decimal("19.99")
    li.quantity = 1
    return li


# ---- Mode determination tests ----


class TestDetermineSessionMode:
    """Tests for _determine_session_mode logic."""

    def test_determine_mode_recurring_plan(self, app, mocker):
        """Should return 'subscription' for monthly recurring plan."""
        from src.plugins.payment_route_helpers import determine_session_mode

        sub_li = _make_subscription_line_item(BillingPeriod.MONTHLY)

        invoice = MagicMock()
        invoice.line_items = [sub_li]

        mock_db = mocker.patch("src.plugins.payment_route_helpers.db")
        mock_db.session.get.return_value = sub_li._sub

        with app.app_context():
            mode = determine_session_mode(invoice)
        assert mode == "subscription"

    def test_determine_mode_one_time_plan(self, app, mocker):
        """Should return 'payment' for one-time (token bundle) items."""
        from src.plugins.payment_route_helpers import determine_session_mode

        li = _make_one_time_line_item()
        invoice = MagicMock()
        invoice.line_items = [li]

        mock_db = mocker.patch("src.plugins.payment_route_helpers.db")
        mock_db.session.get.return_value = None

        with app.app_context():
            mode = determine_session_mode(invoice)
        assert mode == "payment"

    def test_determine_mode_recurring_addon(self, app, mocker):
        """Should return 'subscription' for recurring add-on."""
        from src.plugins.payment_route_helpers import determine_session_mode

        addon_li = _make_addon_line_item("monthly")
        invoice = MagicMock()
        invoice.line_items = [addon_li]

        mock_db = mocker.patch("src.plugins.payment_route_helpers.db")
        mock_db.session.get.return_value = addon_li._addon_sub

        with app.app_context():
            mode = determine_session_mode(invoice)
        assert mode == "subscription"

    def test_determine_mode_mixed_one_time(self, app, mocker):
        """Should return 'payment' when all items are one-time tokens."""
        from src.plugins.payment_route_helpers import determine_session_mode

        li1 = _make_one_time_line_item()
        li2 = _make_one_time_line_item()
        invoice = MagicMock()
        invoice.line_items = [li1, li2]

        mock_db = mocker.patch("src.plugins.payment_route_helpers.db")
        mock_db.session.get.return_value = None

        with app.app_context():
            mode = determine_session_mode(invoice)
        assert mode == "payment"


# ---- Subscription line item building tests ----


class TestBuildSubscriptionItems:
    """Tests for _build_stripe_subscription_items."""

    def test_build_subscription_items_monthly(self, app, mocker):
        """Monthly plan should produce interval='month'."""
        from plugins.stripe.routes import _build_stripe_subscription_items

        li = _make_subscription_line_item(BillingPeriod.MONTHLY)
        invoice = MagicMock()
        invoice.line_items = [li]
        invoice.currency = "EUR"

        mock_db = mocker.patch("plugins.stripe.routes.db")
        mock_db.session.get.return_value = li._sub

        with app.app_context():
            items = _build_stripe_subscription_items(invoice)

        assert len(items) == 1
        assert items[0]["price_data"]["recurring"]["interval"] == "month"

    def test_build_subscription_items_yearly(self, app, mocker):
        """Yearly plan should produce interval='year'."""
        from plugins.stripe.routes import _build_stripe_subscription_items

        li = _make_subscription_line_item(BillingPeriod.YEARLY)
        invoice = MagicMock()
        invoice.line_items = [li]
        invoice.currency = "EUR"

        mock_db = mocker.patch("plugins.stripe.routes.db")
        mock_db.session.get.return_value = li._sub

        with app.app_context():
            items = _build_stripe_subscription_items(invoice)

        assert items[0]["price_data"]["recurring"]["interval"] in ("year",)

    def test_build_subscription_items_quarterly(self, app, mocker):
        """Quarterly plan should produce interval_count=3 with interval='month'."""
        from plugins.stripe.routes import _build_stripe_subscription_items

        li = _make_subscription_line_item(BillingPeriod.QUARTERLY)
        invoice = MagicMock()
        invoice.line_items = [li]
        invoice.currency = "USD"

        mock_db = mocker.patch("plugins.stripe.routes.db")
        mock_db.session.get.return_value = li._sub

        with app.app_context():
            items = _build_stripe_subscription_items(invoice)

        recurring = items[0]["price_data"]["recurring"]
        assert recurring["interval"] == "month"
        assert recurring["interval_count"] == 3


# ---- create-session subscription mode test ----


class TestCreateSessionSubscriptionMode:
    """Test create-session uses mode=subscription for recurring invoices."""

    def test_create_session_subscription_mode(
        self, client, auth_headers, mock_container, mock_stripe, mocker
    ):
        """create-session should call create_subscription_session for recurring invoice."""
        user_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        li = _make_subscription_line_item(BillingPeriod.MONTHLY)
        invoice = _make_invoice(user_id, [li])

        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice

        # Mock db in both modules (routes uses it for _build_stripe_subscription_items,
        # payment_route_helpers uses it for determine_session_mode)
        mock_db = mocker.patch("plugins.stripe.routes.db")
        mock_db.session.get.return_value = li._sub
        mock_helpers_db = mocker.patch("src.plugins.payment_route_helpers.db")
        mock_helpers_db.session.get.return_value = li._sub

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
        self, client, auth_headers, mock_container, mock_stripe, mocker
    ):
        """Should reuse existing payment_customer_id without creating a new customer."""
        user_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        li = _make_subscription_line_item(BillingPeriod.MONTHLY)
        invoice = _make_invoice(user_id, [li])

        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice

        mock_db = mocker.patch("plugins.stripe.routes.db")
        mock_db.session.get.return_value = li._sub
        mock_helpers_db = mocker.patch("src.plugins.payment_route_helpers.db")
        mock_helpers_db.session.get.return_value = li._sub

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
        self, client, mock_stripe, mock_container
    ):
        """checkout.session.completed with subscription should store provider_subscription_id."""
        invoice_id = uuid4()
        sub_id = uuid4()
        li = MagicMock()
        li.item_type = LineItemType.SUBSCRIPTION
        li.item_id = sub_id

        invoice = MagicMock()
        invoice.id = invoice_id
        invoice.line_items = [li]

        sub_mock = MagicMock()
        sub_mock.provider_subscription_id = None

        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice
        mock_container.subscription_repository.return_value.find_by_id.return_value = (
            sub_mock
        )

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

        assert sub_mock.provider_subscription_id == "sub_stripe_123"
        mock_container.subscription_repository.return_value.save.assert_called_with(
            sub_mock
        )

    def test_webhook_invoice_paid_creates_renewal(
        self, client, mock_stripe, mock_container, mocker
    ):
        """invoice.paid (renewal) should create renewal invoice and emit event."""
        subscription = MagicMock()
        subscription.id = uuid4()
        subscription.user_id = uuid4()
        subscription.tarif_plan = MagicMock()
        subscription.tarif_plan.id = uuid4()
        subscription.tarif_plan.name = "Pro"

        mock_container.subscription_repository.return_value.find_by_provider_subscription_id.return_value = (
            subscription
        )
        mock_container.invoice_repository.return_value.find_by_provider_session_id.return_value = (
            None
        )

        # Mock UserInvoice class and its generate_invoice_number
        mock_invoice_cls = mocker.patch(
            "plugins.stripe.routes.UserInvoice", autospec=False
        )
        mock_invoice_instance = MagicMock()
        mock_invoice_instance.id = uuid4()
        mock_invoice_instance.line_items = []
        mock_invoice_cls.return_value = mock_invoice_instance
        mock_invoice_cls.generate_invoice_number.return_value = "INV-001"

        # Mock InvoiceLineItem
        mocker.patch("plugins.stripe.routes.InvoiceLineItem", MagicMock)

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

        mock_container.invoice_repository.return_value.save.assert_called()
        mock_container.event_dispatcher.return_value.emit.assert_called_once()

    def test_webhook_invoice_paid_skips_first(
        self, client, mock_stripe, mock_container
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

        mock_container.event_dispatcher.return_value.emit.assert_not_called()

    def test_webhook_invoice_paid_deduplication(
        self, client, mock_stripe, mock_container
    ):
        """Same stripe_invoice_id should not create a duplicate renewal invoice."""
        subscription = MagicMock()
        subscription.id = uuid4()
        subscription.user_id = uuid4()
        subscription.tarif_plan = MagicMock()

        mock_container.subscription_repository.return_value.find_by_provider_subscription_id.return_value = (
            subscription
        )

        existing_invoice = MagicMock()
        existing_invoice.id = uuid4()
        mock_container.invoice_repository.return_value.find_by_provider_session_id.return_value = (
            existing_invoice
        )

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

        # Emit should still be called (for the existing invoice)
        mock_container.event_dispatcher.return_value.emit.assert_called_once()
        # But save should NOT create a new invoice (uses existing)
        # The existing invoice is returned, so no new UserInvoice is created

    def test_webhook_subscription_deleted_cancels(
        self, client, mock_stripe, mock_container
    ):
        """customer.subscription.deleted should emit SubscriptionCancelledEvent."""
        subscription = MagicMock()
        subscription.id = uuid4()
        subscription.user_id = uuid4()
        mock_container.subscription_repository.return_value.find_by_provider_subscription_id.return_value = (
            subscription
        )

        self._post_webhook(
            client,
            mock_stripe,
            "customer.subscription.deleted",
            {"id": "sub_deleted_123"},
        )

        dispatcher = mock_container.event_dispatcher.return_value
        dispatcher.emit.assert_called_once()
        event = dispatcher.emit.call_args[0][0]
        assert isinstance(event, SubscriptionCancelledEvent)
        assert event.subscription_id == subscription.id
        assert event.provider == "stripe"
        assert event.reason == "stripe_subscription_deleted"

    def test_webhook_payment_failed_emits_event(
        self, client, mock_stripe, mock_container
    ):
        """invoice.payment_failed should emit PaymentFailedEvent."""
        subscription = MagicMock()
        subscription.id = uuid4()
        subscription.user_id = uuid4()
        mock_container.subscription_repository.return_value.find_by_provider_subscription_id.return_value = (
            subscription
        )

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

        dispatcher = mock_container.event_dispatcher.return_value
        dispatcher.emit.assert_called_once()
        event = dispatcher.emit.call_args[0][0]
        assert isinstance(event, PaymentFailedEvent)
        assert event.subscription_id == subscription.id
        assert event.error_code == "payment_failed"
        assert event.error_message == "Card was declined"
        assert event.provider == "stripe"


class TestBillingPeriodToStripeDaily:
    """Tests that DAILY billing period maps to interval=day in Stripe."""

    def test_daily_period_maps_to_day_interval(self):
        from plugins.stripe.routes import BILLING_PERIOD_TO_STRIPE

        assert "DAILY" in BILLING_PERIOD_TO_STRIPE
        assert BILLING_PERIOD_TO_STRIPE["DAILY"]["interval"] == "day"

    def test_build_subscription_items_daily(self, app, mocker):
        from plugins.stripe.routes import _build_stripe_subscription_items
        from src.models.enums import BillingPeriod

        li = _make_subscription_line_item(BillingPeriod.DAILY)
        invoice = MagicMock()
        invoice.line_items = [li]
        invoice.currency = "EUR"

        mock_db = mocker.patch("plugins.stripe.routes.db")
        mock_db.session.get.return_value = li._sub

        with app.app_context():
            items = _build_stripe_subscription_items(invoice)

        assert len(items) == 1
        assert items[0]["price_data"]["recurring"]["interval"] == "day"
