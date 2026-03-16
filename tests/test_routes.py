"""Tests for Stripe plugin API routes."""
import sys
import json
import pytest
from decimal import Decimal
from uuid import uuid4, UUID
from unittest.mock import MagicMock, patch

from flask import Flask, g

from src.plugins.config_store import PluginConfigEntry
from src.models.enums import InvoiceStatus, LineItemType


@pytest.fixture
def mock_stripe(mocker):
    """Mock stripe module in sys.modules."""
    mock_mod = mocker.MagicMock()
    mock_mod.error.SignatureVerificationError = type(
        "SignatureVerificationError", (Exception,), {}
    )
    mock_mod.error.StripeError = type("StripeError", (Exception,), {})
    mocker.patch.dict(sys.modules, {"stripe": mock_mod})
    return mock_mod


@pytest.fixture
def stripe_config():
    """Stripe config values."""
    return {
        "test_publishable_key": "pk_test_abc",
        "test_secret_key": "sk_test_secret",
        "test_webhook_secret": "whsec_test",
        "sandbox": True,
    }


@pytest.fixture
def mock_config_store(mocker, stripe_config):
    """Config store returning enabled Stripe plugin."""
    store = mocker.MagicMock()
    store.get_by_name.return_value = PluginConfigEntry(
        plugin_name="stripe", status="enabled", config=stripe_config
    )
    store.get_config.return_value = stripe_config
    return store


@pytest.fixture
def mock_config_store_disabled(mocker):
    """Config store returning disabled Stripe plugin."""
    store = mocker.MagicMock()
    store.get_by_name.return_value = PluginConfigEntry(
        plugin_name="stripe", status="disabled"
    )
    return store


@pytest.fixture
def mock_container(mocker):
    """Mock DI container with repositories and event dispatcher."""
    container = mocker.MagicMock()
    container.invoice_repository.return_value = mocker.MagicMock()
    container.subscription_repository.return_value = mocker.MagicMock()
    container.user_repository.return_value = mocker.MagicMock()
    container.addon_subscription_repository.return_value = mocker.MagicMock()
    container.event_dispatcher.return_value = mocker.MagicMock()
    return container


@pytest.fixture
def app(mock_stripe, mock_config_store, mock_container, mocker):
    """Create Flask app with Stripe blueprint registered."""
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

    mock_db = MagicMock()
    mocker.patch("src.middleware.auth.db", mock_db)

    from plugins.stripe.routes import stripe_plugin_bp

    flask_app.register_blueprint(stripe_plugin_bp, url_prefix="/api/v1/plugins/stripe")

    flask_app.config_store = mock_config_store
    flask_app.container = mock_container

    return flask_app


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def auth_headers():
    """Auth headers with a dummy bearer token."""
    return {"Authorization": "Bearer test_token_123"}


# ---- create-session tests ----


class TestCreateSession:
    """Tests for POST /api/v1/plugins/stripe/create-session."""

    def test_create_session_requires_auth(self, client):
        """Should return 401 without Authorization header."""
        resp = client.post(
            "/api/v1/plugins/stripe/create-session",
            json={"invoice_id": str(uuid4())},
        )
        assert resp.status_code == 401

    def test_create_session_plugin_disabled(
        self, client, app, mock_config_store_disabled, auth_headers
    ):
        """Should return 404 when plugin is disabled."""
        app.config_store = mock_config_store_disabled
        resp = client.post(
            "/api/v1/plugins/stripe/create-session",
            json={"invoice_id": str(uuid4())},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_create_session_missing_invoice_id(self, client, auth_headers, mock_container):
        """Should return 400 when invoice_id is missing or empty."""
        mock_container.invoice_repository.return_value.find_by_id.return_value = None
        resp = client.post(
            "/api/v1/plugins/stripe/create-session",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_session_invoice_not_found(self, client, auth_headers, mock_container):
        """Should return 404 when invoice does not exist."""
        mock_container.invoice_repository.return_value.find_by_id.return_value = None
        resp = client.post(
            "/api/v1/plugins/stripe/create-session",
            json={"invoice_id": str(uuid4())},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_create_session_invoice_not_pending(self, client, auth_headers, mock_container):
        """Should return 400 when invoice status is not pending."""
        invoice = MagicMock()
        invoice.id = uuid4()
        invoice.status = InvoiceStatus.PAID
        invoice.user_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice

        resp = client.post(
            "/api/v1/plugins/stripe/create-session",
            json={"invoice_id": str(invoice.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_session_wrong_user(self, client, auth_headers, mock_container):
        """Should return 403 when invoice belongs to a different user."""
        invoice = MagicMock()
        invoice.id = uuid4()
        invoice.status = InvoiceStatus.PENDING
        invoice.user_id = uuid4()  # Different from auth user
        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice

        resp = client.post(
            "/api/v1/plugins/stripe/create-session",
            json={"invoice_id": str(invoice.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 403

    def test_create_session_success(self, client, auth_headers, mock_container, mock_stripe):
        """Should return 200 with session_id and session_url on success."""
        user_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        invoice = MagicMock()
        invoice.id = uuid4()
        invoice.status = InvoiceStatus.PENDING
        invoice.user_id = user_id
        invoice.total_amount = Decimal("29.99")
        invoice.amount = Decimal("29.99")
        invoice.currency = "EUR"
        invoice.line_items = []  # No recurring items => mode=payment

        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice

        # Mock Stripe Session.create for the adapter
        mock_session = MagicMock()
        mock_session.id = "cs_test_ok"
        mock_session.url = "https://checkout.stripe.com/cs_test_ok"
        mock_stripe.checkout.Session.create.return_value = mock_session

        resp = client.post(
            "/api/v1/plugins/stripe/create-session",
            json={"invoice_id": str(invoice.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["session_id"] == "cs_test_ok"
        assert "session_url" in data


# ---- webhook tests ----


class TestWebhook:
    """Tests for POST /api/v1/plugins/stripe/webhook."""

    def test_webhook_invalid_signature(self, client, mock_stripe):
        """Should return 400 when Stripe signature verification fails."""
        mock_stripe.Webhook.construct_event.side_effect = (
            mock_stripe.error.SignatureVerificationError("bad sig")
        )

        resp = client.post(
            "/api/v1/plugins/stripe/webhook",
            data=b"raw_body",
            headers={"Stripe-Signature": "invalid_sig"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_webhook_plugin_disabled(self, client, app, mock_config_store_disabled):
        """Should return 404 when plugin is disabled."""
        app.config_store = mock_config_store_disabled
        resp = client.post(
            "/api/v1/plugins/stripe/webhook",
            data=b"raw_body",
            headers={"Stripe-Signature": "sig"},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_webhook_checkout_completed(self, client, mock_stripe, mock_container):
        """Should return 200 and emit event for checkout.session.completed."""
        invoice_id = str(uuid4())
        mock_stripe.Webhook.construct_event.return_value = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_abc",
                    "metadata": {"invoice_id": invoice_id, "user_id": str(uuid4())},
                    "amount_total": 2999,
                    "currency": "eur",
                    "payment_intent": "pi_test_xyz",
                    "subscription": None,
                },
            },
        }

        resp = client.post(
            "/api/v1/plugins/stripe/webhook",
            data=b"raw_body",
            headers={"Stripe-Signature": "valid_sig"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["received"] is True
        # Event emitted via container.event_dispatcher().emit()
        mock_container.event_dispatcher.return_value.emit.assert_called_once()

    def test_webhook_ignores_unknown_events(self, client, mock_stripe, mock_container):
        """Should return 200 but emit no event for unknown event types."""
        mock_stripe.Webhook.construct_event.return_value = {
            "type": "some.unknown.event",
            "data": {"object": {}},
        }

        resp = client.post(
            "/api/v1/plugins/stripe/webhook",
            data=b"raw_body",
            headers={"Stripe-Signature": "valid_sig"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        mock_container.event_dispatcher.return_value.emit.assert_not_called()


# ---- session-status tests ----


class TestSessionStatus:
    """Tests for GET /api/v1/plugins/stripe/session-status/<session_id>."""

    def test_session_status_requires_auth(self, client):
        """Should return 401 without Authorization header."""
        resp = client.get("/api/v1/plugins/stripe/session-status/cs_test_123")
        assert resp.status_code == 401

    def test_session_status_success(self, client, auth_headers, mock_stripe):
        """Should return 200 with status data."""
        mock_session = MagicMock()
        mock_session.payment_status = "paid"
        mock_session.amount_total = 2999
        mock_session.currency = "eur"
        mock_stripe.checkout.Session.retrieve.return_value = mock_session

        resp = client.get(
            "/api/v1/plugins/stripe/session-status/cs_test_123",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "paid"
        assert data["amount_total"] == 2999
