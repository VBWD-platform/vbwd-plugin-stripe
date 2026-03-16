"""End-to-end integration tests for Stripe payment flow.

Tests the FULL pipeline:
  Webhook arrives → signature verified → PaymentCapturedEvent emitted
  → PaymentCapturedHandler runs → invoice marked PAID,
    subscription activated, tokens credited, add-ons activated.

These tests wire up a real DomainEventDispatcher with the real
PaymentCapturedHandler, using mock repositories. This verifies
the event system actually connects webhook → handler → domain changes.
"""
import sys
import json
import pytest
from uuid import uuid4, UUID
from decimal import Decimal
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock

from flask import Flask

from src.events.domain import DomainEventDispatcher
from src.events.payment_events import (
    PaymentCapturedEvent,
    PaymentRefundedEvent,
    RefundReversedEvent,
    SubscriptionCancelledEvent,
    PaymentFailedEvent,
)
from src.handlers.payment_handler import PaymentCapturedHandler
from src.plugins.config_store import PluginConfigEntry
from src.models.enums import (
    InvoiceStatus,
    LineItemType,
    SubscriptionStatus,
    PurchaseStatus,
    BillingPeriod,
)

# Pre-inject stripe mock into sys.modules BEFORE importing routes,
# so that the lazy `import stripe` inside webhook handler works.
# This avoids mocker.patch.dict(sys.modules, ...) which restores the
# entire dict on teardown, removing bcrypt and causing PyO3 errors.
_stripe_mock = MagicMock()
_stripe_mock.error.SignatureVerificationError = type(
    "SignatureVerificationError", (Exception,), {}
)
if "stripe" not in sys.modules:
    sys.modules["stripe"] = _stripe_mock

# Import blueprint at module level (safe now that stripe is mocked).
# This prevents repeated imports that trigger bcrypt PyO3 re-init errors.
from plugins.stripe.routes import stripe_plugin_bp


# ---------------------------------------------------------------------------
# Helpers to create realistic mock domain objects
# ---------------------------------------------------------------------------

def _make_invoice(invoice_id, user_id, line_items=None, status="PENDING"):
    inv = MagicMock()
    inv.id = invoice_id
    inv.user_id = user_id
    inv.status = InvoiceStatus(status)
    inv.payment_ref = None
    inv.paid_at = None
    inv.total_amount = Decimal("29.99")
    inv.amount = Decimal("29.99")
    inv.currency = "EUR"
    inv.line_items = line_items or []
    return inv


def _make_line_item(item_type, item_id, unit_price=Decimal("29.99")):
    li = MagicMock()
    li.item_type = item_type
    li.item_id = item_id
    li.unit_price = unit_price
    li.quantity = 1
    li.total_price = unit_price
    return li


def _make_subscription(sub_id, user_id, status=SubscriptionStatus.PENDING, is_recurring=True):
    sub = MagicMock()
    sub.id = sub_id
    sub.user_id = user_id
    sub.status = status
    sub.starts_at = None
    sub.expires_at = None
    sub.cancelled_at = None
    sub.provider_subscription_id = None
    plan = MagicMock()
    plan.name = "Pro Plan"
    plan.is_recurring = is_recurring
    plan.billing_period = BillingPeriod.MONTHLY
    sub.tarif_plan = plan
    return sub


def _make_token_purchase(purchase_id, token_amount=100, status=PurchaseStatus.PENDING):
    p = MagicMock()
    p.id = purchase_id
    p.status = status
    p.token_amount = token_amount
    p.completed_at = None
    p.tokens_credited = False
    return p


def _make_addon_sub(addon_id, status=SubscriptionStatus.PENDING):
    a = MagicMock()
    a.id = addon_id
    a.status = status
    a.activated_at = None
    return a


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_stripe():
    """Return the module-level stripe mock, reset between tests."""
    _stripe_mock.reset_mock()
    # Re-attach exception class after reset
    _stripe_mock.error.SignatureVerificationError = type(
        "SignatureVerificationError", (Exception,), {}
    )
    return _stripe_mock


@pytest.fixture
def stripe_config():
    return {
        "test_publishable_key": "pk_test",
        "test_secret_key": "sk_test",
        "test_webhook_secret": "whsec_test",
        "sandbox": True,
    }


@pytest.fixture
def mock_config_store(stripe_config):
    store = MagicMock()
    store.get_by_name.return_value = PluginConfigEntry(
        plugin_name="stripe", status="enabled", config=stripe_config
    )
    store.get_config.return_value = stripe_config
    return store


@pytest.fixture
def ids():
    """Bundle of UUIDs used across tests."""
    return {
        "user": uuid4(),
        "invoice": uuid4(),
        "subscription": uuid4(),
        "token_purchase": uuid4(),
        "addon": uuid4(),
    }


@pytest.fixture
def mock_repos(ids):
    """Mock repositories that return realistic domain objects."""
    repos = {}
    for name in [
        "invoice_repository",
        "subscription_repository",
        "token_balance_repository",
        "token_transaction_repository",
        "token_bundle_purchase_repository",
        "addon_subscription_repository",
        "user_repository",
    ]:
        repos[name] = MagicMock()
    return repos


@pytest.fixture
def container_with_real_dispatcher(mock_repos):
    """DI container with REAL DomainEventDispatcher + PaymentCapturedHandler wired."""
    container = MagicMock()

    # Wire up mock repos
    for name, repo in mock_repos.items():
        getattr(container, name).return_value = repo

    # Real dispatcher with real handler
    dispatcher = DomainEventDispatcher()
    handler = PaymentCapturedHandler(container)
    dispatcher.register("payment.captured", handler)

    container.event_dispatcher.return_value = dispatcher
    return container


@pytest.fixture
def app(mock_stripe, mock_config_store, container_with_real_dispatcher, mocker):
    """Flask app with stripe blueprint, real event dispatcher, mock auth."""
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True

    user_id = uuid4()
    mock_auth_service = MagicMock()
    mock_auth_service.return_value.verify_token.return_value = str(user_id)
    mocker.patch("src.middleware.auth.AuthService", mock_auth_service)

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.status.value = "ACTIVE"
    mock_user_repo = MagicMock()
    mock_user_repo.return_value.find_by_id.return_value = mock_user
    mocker.patch("src.middleware.auth.UserRepository", mock_user_repo)
    mocker.patch("src.middleware.auth.db", MagicMock())

    # Use module-level blueprint (already imported at top of file)
    flask_app.register_blueprint(stripe_plugin_bp, url_prefix="/api/v1/plugins/stripe")
    flask_app.config_store = mock_config_store
    flask_app.container = container_with_real_dispatcher
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _post_webhook(client, mock_stripe, event_type, obj):
    """Helper to POST a Stripe webhook event."""
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


# ===========================================================================
# Test: checkout.session.completed → invoice PAID + subscription ACTIVE
# ===========================================================================

class TestCheckoutCompletedE2E:
    """Full pipeline: webhook → event → handler marks invoice paid & activates sub."""

    def test_invoice_marked_paid_after_webhook(
        self, client, mock_stripe, mock_repos, ids
    ):
        """After checkout.session.completed, invoice.status should become PAID."""
        sub = _make_subscription(ids["subscription"], ids["user"])
        li = _make_line_item(LineItemType.SUBSCRIPTION, ids["subscription"])
        invoice = _make_invoice(ids["invoice"], ids["user"], [li])

        mock_repos["invoice_repository"].find_by_id.return_value = invoice
        mock_repos["subscription_repository"].find_by_id.return_value = sub
        mock_repos["subscription_repository"].find_active_by_user.return_value = None

        checkout_obj = {
            "id": "cs_test_abc",
            "metadata": {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])},
            "amount_total": 2999,
            "currency": "eur",
            "payment_intent": "pi_test_xyz",
            "subscription": None,
        }

        resp = _post_webhook(client, mock_stripe, "checkout.session.completed", checkout_obj)
        assert resp.status_code == 200

        # Invoice should be marked as paid
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.payment_ref == "cs_test_abc"
        assert invoice.paid_at is not None
        mock_repos["invoice_repository"].save.assert_called_with(invoice)

    def test_subscription_activated_after_webhook(
        self, client, mock_stripe, mock_repos, ids
    ):
        """After checkout.session.completed, subscription should become ACTIVE."""
        sub = _make_subscription(ids["subscription"], ids["user"])
        li = _make_line_item(LineItemType.SUBSCRIPTION, ids["subscription"])
        invoice = _make_invoice(ids["invoice"], ids["user"], [li])

        mock_repos["invoice_repository"].find_by_id.return_value = invoice
        mock_repos["subscription_repository"].find_by_id.return_value = sub
        mock_repos["subscription_repository"].find_active_by_user.return_value = None

        checkout_obj = {
            "id": "cs_test_sub",
            "metadata": {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])},
            "amount_total": 2999,
            "currency": "eur",
            "payment_intent": "pi_sub",
            "subscription": None,
        }

        _post_webhook(client, mock_stripe, "checkout.session.completed", checkout_obj)

        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.starts_at is not None
        assert sub.expires_at is not None
        mock_repos["subscription_repository"].save.assert_called()

    def test_previous_subscription_cancelled(
        self, client, mock_stripe, mock_repos, ids
    ):
        """If user has an existing ACTIVE subscription, it should be cancelled."""
        new_sub = _make_subscription(ids["subscription"], ids["user"])
        old_sub = _make_subscription(uuid4(), ids["user"], status=SubscriptionStatus.ACTIVE)

        li = _make_line_item(LineItemType.SUBSCRIPTION, ids["subscription"])
        invoice = _make_invoice(ids["invoice"], ids["user"], [li])

        mock_repos["invoice_repository"].find_by_id.return_value = invoice
        mock_repos["subscription_repository"].find_by_id.return_value = new_sub
        mock_repos["subscription_repository"].find_active_by_user.return_value = old_sub

        checkout_obj = {
            "id": "cs_upgrade",
            "metadata": {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])},
            "amount_total": 4999,
            "currency": "eur",
            "payment_intent": "pi_upgrade",
            "subscription": None,
        }

        _post_webhook(client, mock_stripe, "checkout.session.completed", checkout_obj)

        # Old subscription should be cancelled
        assert old_sub.status == SubscriptionStatus.CANCELLED
        assert old_sub.cancelled_at is not None
        # New subscription should be active
        assert new_sub.status == SubscriptionStatus.ACTIVE


# ===========================================================================
# Test: checkout.session.completed → token bundle credited
# ===========================================================================

class TestTokenBundlePurchaseE2E:
    """Full pipeline: webhook → event → handler credits tokens."""

    def test_tokens_credited_after_webhook(
        self, client, mock_stripe, mock_repos, ids
    ):
        """Token purchase completed → tokens added to balance."""
        purchase = _make_token_purchase(ids["token_purchase"], token_amount=500)
        li = _make_line_item(LineItemType.TOKEN_BUNDLE, ids["token_purchase"], Decimal("9.99"))
        invoice = _make_invoice(ids["invoice"], ids["user"], [li])
        invoice.total_amount = Decimal("9.99")

        balance = MagicMock()
        balance.balance = 100  # existing balance

        mock_repos["invoice_repository"].find_by_id.return_value = invoice
        mock_repos["token_bundle_purchase_repository"].find_by_id.return_value = purchase
        mock_repos["token_balance_repository"].find_by_user_id.return_value = balance

        checkout_obj = {
            "id": "cs_tokens",
            "metadata": {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])},
            "amount_total": 999,
            "currency": "eur",
            "payment_intent": "pi_tokens",
            "subscription": None,
        }

        _post_webhook(client, mock_stripe, "checkout.session.completed", checkout_obj)

        # Invoice should be paid
        assert invoice.status == InvoiceStatus.PAID

        # Purchase marked completed
        assert purchase.status == PurchaseStatus.COMPLETED
        assert purchase.tokens_credited is True

        # Balance credited
        assert balance.balance == 600  # 100 + 500
        mock_repos["token_balance_repository"].save.assert_called()
        mock_repos["token_transaction_repository"].save.assert_called_once()

    def test_new_balance_created_if_none_exists(
        self, client, mock_stripe, mock_repos, ids
    ):
        """If user has no token balance, one should be created."""
        purchase = _make_token_purchase(ids["token_purchase"], token_amount=200)
        li = _make_line_item(LineItemType.TOKEN_BUNDLE, ids["token_purchase"])
        invoice = _make_invoice(ids["invoice"], ids["user"], [li])

        mock_repos["invoice_repository"].find_by_id.return_value = invoice
        mock_repos["token_bundle_purchase_repository"].find_by_id.return_value = purchase
        mock_repos["token_balance_repository"].find_by_user_id.return_value = None

        checkout_obj = {
            "id": "cs_newtokens",
            "metadata": {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])},
            "amount_total": 2999,
            "currency": "eur",
            "payment_intent": "pi_newtokens",
            "subscription": None,
        }

        _post_webhook(client, mock_stripe, "checkout.session.completed", checkout_obj)

        # save should be called with a new balance object
        saved_balance = mock_repos["token_balance_repository"].save.call_args[0][0]
        assert saved_balance.balance == 200


# ===========================================================================
# Test: checkout.session.completed → add-on activated
# ===========================================================================

class TestAddOnActivationE2E:
    """Full pipeline: webhook → event → handler activates add-on subscription."""

    def test_addon_activated_after_webhook(
        self, client, mock_stripe, mock_repos, ids
    ):
        """Add-on subscription should become ACTIVE after payment."""
        addon_sub = _make_addon_sub(ids["addon"])
        li = _make_line_item(LineItemType.ADD_ON, ids["addon"], Decimal("4.99"))
        invoice = _make_invoice(ids["invoice"], ids["user"], [li])

        mock_repos["invoice_repository"].find_by_id.return_value = invoice
        mock_repos["addon_subscription_repository"].find_by_id.return_value = addon_sub

        checkout_obj = {
            "id": "cs_addon",
            "metadata": {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])},
            "amount_total": 499,
            "currency": "eur",
            "payment_intent": "pi_addon",
            "subscription": None,
        }

        _post_webhook(client, mock_stripe, "checkout.session.completed", checkout_obj)

        assert invoice.status == InvoiceStatus.PAID
        assert addon_sub.status == SubscriptionStatus.ACTIVE
        assert addon_sub.activated_at is not None


# ===========================================================================
# Test: Mixed invoice (subscription + tokens + add-on)
# ===========================================================================

class TestMixedInvoiceE2E:
    """Full pipeline with invoice containing multiple line item types."""

    def test_all_items_activated_in_single_webhook(
        self, client, mock_stripe, mock_repos, ids
    ):
        """One webhook should activate subscription, credit tokens, and activate add-on."""
        sub = _make_subscription(ids["subscription"], ids["user"])
        purchase = _make_token_purchase(ids["token_purchase"], token_amount=300)
        addon_sub = _make_addon_sub(ids["addon"])

        line_items = [
            _make_line_item(LineItemType.SUBSCRIPTION, ids["subscription"], Decimal("19.99")),
            _make_line_item(LineItemType.TOKEN_BUNDLE, ids["token_purchase"], Decimal("5.99")),
            _make_line_item(LineItemType.ADD_ON, ids["addon"], Decimal("3.99")),
        ]
        invoice = _make_invoice(ids["invoice"], ids["user"], line_items)

        balance = MagicMock()
        balance.balance = 50

        mock_repos["invoice_repository"].find_by_id.return_value = invoice
        mock_repos["subscription_repository"].find_by_id.return_value = sub
        mock_repos["subscription_repository"].find_active_by_user.return_value = None
        mock_repos["token_bundle_purchase_repository"].find_by_id.return_value = purchase
        mock_repos["token_balance_repository"].find_by_user_id.return_value = balance
        mock_repos["addon_subscription_repository"].find_by_id.return_value = addon_sub

        checkout_obj = {
            "id": "cs_mixed",
            "metadata": {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])},
            "amount_total": 2997,
            "currency": "eur",
            "payment_intent": "pi_mixed",
            "subscription": None,
        }

        resp = _post_webhook(client, mock_stripe, "checkout.session.completed", checkout_obj)
        assert resp.status_code == 200

        # All items activated
        assert invoice.status == InvoiceStatus.PAID
        assert sub.status == SubscriptionStatus.ACTIVE
        assert purchase.status == PurchaseStatus.COMPLETED
        assert balance.balance == 350  # 50 + 300
        assert addon_sub.status == SubscriptionStatus.ACTIVE


# ===========================================================================
# Test: Idempotency — webhook fires twice
# ===========================================================================

class TestIdempotencyE2E:
    """Verify double webhook delivery doesn't cause double activation."""

    def test_already_paid_invoice_not_re_saved(
        self, client, mock_stripe, mock_repos, ids
    ):
        """If invoice is already PAID, handler should not overwrite paid_at."""
        li = _make_line_item(LineItemType.SUBSCRIPTION, ids["subscription"])
        invoice = _make_invoice(ids["invoice"], ids["user"], [li], status="PAID")
        original_paid_at = datetime(2026, 2, 11, 12, 0, 0)
        invoice.paid_at = original_paid_at

        sub = _make_subscription(
            ids["subscription"], ids["user"], status=SubscriptionStatus.ACTIVE
        )

        mock_repos["invoice_repository"].find_by_id.return_value = invoice
        mock_repos["subscription_repository"].find_by_id.return_value = sub

        checkout_obj = {
            "id": "cs_dupe",
            "metadata": {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])},
            "amount_total": 2999,
            "currency": "eur",
            "payment_intent": "pi_dupe",
            "subscription": None,
        }

        _post_webhook(client, mock_stripe, "checkout.session.completed", checkout_obj)

        # Invoice paid_at should not be overwritten
        assert invoice.paid_at == original_paid_at
        # Subscription should NOT be re-activated (already ACTIVE, not PENDING)
        mock_repos["subscription_repository"].save.assert_not_called()


# ===========================================================================
# Test: Stripe subscription linking
# ===========================================================================

class TestStripeSubscriptionLinking:
    """Verify provider_subscription_id is stored on our Subscription model."""

    def test_stripe_sub_id_linked_on_checkout(
        self, client, mock_stripe, mock_repos, ids
    ):
        """checkout.session.completed with subscription should link provider_subscription_id."""
        sub = _make_subscription(ids["subscription"], ids["user"])
        li = _make_line_item(LineItemType.SUBSCRIPTION, ids["subscription"])
        invoice = _make_invoice(ids["invoice"], ids["user"], [li])

        mock_repos["invoice_repository"].find_by_id.return_value = invoice
        mock_repos["subscription_repository"].find_by_id.return_value = sub
        mock_repos["subscription_repository"].find_active_by_user.return_value = None

        checkout_obj = {
            "id": "cs_recurring",
            "metadata": {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])},
            "amount_total": 2999,
            "currency": "eur",
            "payment_intent": "pi_recurring",
            "subscription": "sub_stripe_123",
        }

        _post_webhook(client, mock_stripe, "checkout.session.completed", checkout_obj)

        # provider_subscription_id should be stored
        assert sub.provider_subscription_id == "sub_stripe_123"


# ===========================================================================
# Test: subscription.deleted webhook → SubscriptionCancelledEvent
# ===========================================================================

class TestSubscriptionDeletedE2E:
    """Verify customer.subscription.deleted emits SubscriptionCancelledEvent."""

    def test_cancellation_event_emitted(
        self, client, mock_stripe, mock_repos, container_with_real_dispatcher, ids
    ):
        """customer.subscription.deleted should emit SubscriptionCancelledEvent."""
        sub = _make_subscription(
            ids["subscription"], ids["user"], status=SubscriptionStatus.ACTIVE
        )
        sub.provider_subscription_id = "sub_stripe_del"
        mock_repos["subscription_repository"].find_by_provider_subscription_id.return_value = sub

        # Track emitted events
        dispatcher = container_with_real_dispatcher.event_dispatcher.return_value
        emitted = []
        original_emit = dispatcher.emit

        def tracking_emit(event):
            emitted.append(event)
            # SubscriptionCancelledEvent has no registered handler, so just return
            if isinstance(event, PaymentCapturedEvent):
                return original_emit(event)
            return None

        dispatcher.emit = tracking_emit

        stripe_sub_obj = {"id": "sub_stripe_del"}

        resp = _post_webhook(client, mock_stripe, "customer.subscription.deleted", stripe_sub_obj)
        assert resp.status_code == 200

        # Should have emitted SubscriptionCancelledEvent
        assert len(emitted) == 1
        assert isinstance(emitted[0], SubscriptionCancelledEvent)
        assert emitted[0].subscription_id == ids["subscription"]
        assert emitted[0].provider == "stripe"


# ===========================================================================
# Test: invoice.payment_failed webhook → PaymentFailedEvent
# ===========================================================================

class TestPaymentFailedE2E:
    """Verify invoice.payment_failed emits PaymentFailedEvent."""

    def test_payment_failed_event_emitted(
        self, client, mock_stripe, mock_repos, container_with_real_dispatcher, ids
    ):
        """invoice.payment_failed should emit PaymentFailedEvent."""
        sub = _make_subscription(
            ids["subscription"], ids["user"], status=SubscriptionStatus.ACTIVE
        )
        mock_repos["subscription_repository"].find_by_provider_subscription_id.return_value = sub

        dispatcher = container_with_real_dispatcher.event_dispatcher.return_value
        emitted = []
        original_emit = dispatcher.emit

        def tracking_emit(event):
            emitted.append(event)
            if isinstance(event, PaymentCapturedEvent):
                return original_emit(event)
            return None

        dispatcher.emit = tracking_emit

        stripe_invoice_obj = {
            "id": "in_fail_123",
            "subscription": "sub_stripe_fail",
            "last_payment_error": {"message": "Card declined"},
        }
        mock_repos["subscription_repository"].find_by_provider_subscription_id.return_value = sub

        resp = _post_webhook(client, mock_stripe, "invoice.payment_failed", stripe_invoice_obj)
        assert resp.status_code == 200

        assert len(emitted) == 1
        assert isinstance(emitted[0], PaymentFailedEvent)
        assert emitted[0].subscription_id == ids["subscription"]
        assert emitted[0].error_message == "Card declined"


# ===========================================================================
# Test: Webhook returns 200 even when handler errors
# ===========================================================================

class TestWebhookResilience:
    """Webhook should always return 200 to prevent Stripe retries on app errors."""

    def test_returns_200_when_invoice_not_found(
        self, client, mock_stripe, mock_repos, ids
    ):
        """Missing invoice should not cause 500 — webhook still returns 200."""
        mock_repos["invoice_repository"].find_by_id.return_value = None

        checkout_obj = {
            "id": "cs_missing",
            "metadata": {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])},
            "amount_total": 100,
            "currency": "eur",
            "payment_intent": "pi_missing",
            "subscription": None,
        }

        resp = _post_webhook(client, mock_stripe, "checkout.session.completed", checkout_obj)
        assert resp.status_code == 200

    def test_returns_200_on_unknown_event_type(
        self, client, mock_stripe, mock_repos
    ):
        """Unknown Stripe event types should be ignored gracefully."""
        resp = _post_webhook(client, mock_stripe, "some.unknown.event", {"id": "obj_123"})
        assert resp.status_code == 200


# ===========================================================================
# Test: charge.refunded webhook → PaymentRefundedEvent
# ===========================================================================

class TestChargeRefundedE2E:
    """Verify charge.refunded emits PaymentRefundedEvent."""

    def test_refund_event_emitted(
        self, client, mock_stripe, mock_repos, container_with_real_dispatcher, ids
    ):
        """charge.refunded should look up session and emit PaymentRefundedEvent."""
        dispatcher = container_with_real_dispatcher.event_dispatcher.return_value
        emitted = []
        original_emit = dispatcher.emit

        def tracking_emit(event):
            emitted.append(event)
            if isinstance(event, PaymentCapturedEvent):
                return original_emit(event)
            return None

        dispatcher.emit = tracking_emit

        # Mock stripe.checkout.Session.list to return session with invoice_id
        mock_session = MagicMock()
        mock_session.metadata = {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])}
        mock_stripe.checkout.Session.list.return_value = MagicMock(data=[mock_session])

        charge_obj = {
            "id": "ch_refund_123",
            "payment_intent": "pi_original_456",
            "amount_refunded": 2999,
            "currency": "eur",
        }

        resp = _post_webhook(client, mock_stripe, "charge.refunded", charge_obj)
        assert resp.status_code == 200

        assert len(emitted) == 1
        assert isinstance(emitted[0], PaymentRefundedEvent)
        assert emitted[0].invoice_id == ids["invoice"]
        assert emitted[0].refund_reference == "ch_refund_123"
        assert emitted[0].amount == "29.99"
        assert emitted[0].currency == "eur"

    def test_refund_skipped_when_no_session(
        self, client, mock_stripe, mock_repos, container_with_real_dispatcher, ids
    ):
        """charge.refunded with no matching session should be ignored."""
        dispatcher = container_with_real_dispatcher.event_dispatcher.return_value
        emitted = []
        dispatcher.emit = lambda event: emitted.append(event) or None

        mock_stripe.checkout.Session.list.return_value = MagicMock(data=[])

        charge_obj = {
            "id": "ch_orphan",
            "payment_intent": "pi_unknown",
            "amount_refunded": 1000,
            "currency": "usd",
        }

        resp = _post_webhook(client, mock_stripe, "charge.refunded", charge_obj)
        assert resp.status_code == 200
        assert len(emitted) == 0


# ===========================================================================
# Test: refund.updated webhook → RefundReversedEvent
# ===========================================================================

class TestRefundUpdatedE2E:
    """Verify refund.updated (status=canceled) emits RefundReversedEvent."""

    def test_refund_reversal_event_emitted(
        self, client, mock_stripe, mock_repos, container_with_real_dispatcher, ids
    ):
        """refund.updated with status=canceled should emit RefundReversedEvent."""
        dispatcher = container_with_real_dispatcher.event_dispatcher.return_value
        emitted = []
        original_emit = dispatcher.emit

        def tracking_emit(event):
            emitted.append(event)
            if isinstance(event, PaymentCapturedEvent):
                return original_emit(event)
            return None

        dispatcher.emit = tracking_emit

        # Mock stripe.checkout.Session.list to return session with invoice_id
        mock_session = MagicMock()
        mock_session.metadata = {"invoice_id": str(ids["invoice"]), "user_id": str(ids["user"])}
        mock_stripe.checkout.Session.list.return_value = MagicMock(data=[mock_session])

        refund_obj = {
            "id": "re_cancel_123",
            "status": "canceled",
            "payment_intent": "pi_original_789",
            "charge": "ch_original_789",
        }

        resp = _post_webhook(client, mock_stripe, "refund.updated", refund_obj)
        assert resp.status_code == 200

        assert len(emitted) == 1
        assert isinstance(emitted[0], RefundReversedEvent)
        assert emitted[0].invoice_id == ids["invoice"]
        assert emitted[0].reason == "stripe_refund_canceled"
        assert emitted[0].provider == "stripe"

    def test_refund_updated_ignored_when_not_canceled(
        self, client, mock_stripe, mock_repos, container_with_real_dispatcher, ids
    ):
        """refund.updated with status != canceled should be ignored."""
        dispatcher = container_with_real_dispatcher.event_dispatcher.return_value
        emitted = []
        dispatcher.emit = lambda event: emitted.append(event) or None

        refund_obj = {
            "id": "re_pending",
            "status": "pending",
            "payment_intent": "pi_pending",
        }

        resp = _post_webhook(client, mock_stripe, "refund.updated", refund_obj)
        assert resp.status_code == 200
        assert len(emitted) == 0

    def test_refund_reversal_via_charge_fallback(
        self, client, mock_stripe, mock_repos, container_with_real_dispatcher, ids
    ):
        """If refund has no payment_intent, fall back to charge lookup."""
        dispatcher = container_with_real_dispatcher.event_dispatcher.return_value
        emitted = []
        original_emit = dispatcher.emit

        def tracking_emit(event):
            emitted.append(event)
            if isinstance(event, PaymentCapturedEvent):
                return original_emit(event)
            return None

        dispatcher.emit = tracking_emit

        # No payment_intent on refund, but has charge
        mock_charge = MagicMock()
        mock_charge.payment_intent = "pi_from_charge"
        mock_stripe.Charge.retrieve.return_value = mock_charge

        mock_session = MagicMock()
        mock_session.metadata = {"invoice_id": str(ids["invoice"])}
        mock_stripe.checkout.Session.list.return_value = MagicMock(data=[mock_session])

        refund_obj = {
            "id": "re_charge_fb",
            "status": "canceled",
            "charge": "ch_for_lookup",
            # No payment_intent
        }

        resp = _post_webhook(client, mock_stripe, "refund.updated", refund_obj)
        assert resp.status_code == 200

        assert len(emitted) == 1
        assert isinstance(emitted[0], RefundReversedEvent)
        assert emitted[0].invoice_id == ids["invoice"]

    def test_refund_reversal_skipped_when_no_session(
        self, client, mock_stripe, mock_repos, container_with_real_dispatcher, ids
    ):
        """refund.updated with no matching checkout session should be ignored."""
        dispatcher = container_with_real_dispatcher.event_dispatcher.return_value
        emitted = []
        dispatcher.emit = lambda event: emitted.append(event) or None

        mock_stripe.checkout.Session.list.return_value = MagicMock(data=[])

        refund_obj = {
            "id": "re_orphan",
            "status": "canceled",
            "payment_intent": "pi_orphan",
        }

        resp = _post_webhook(client, mock_stripe, "refund.updated", refund_obj)
        assert resp.status_code == 200
        assert len(emitted) == 0
