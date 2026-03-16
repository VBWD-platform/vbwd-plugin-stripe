"""Tests verifying event-driven design of Stripe webhook handling.

The Stripe webhook route should ONLY emit domain events and NEVER
mutate domain objects (invoices, subscriptions) directly.
"""
import sys
import json
import pytest
from uuid import uuid4, UUID
from unittest.mock import MagicMock, call

from flask import Flask, g

from src.plugins.config_store import PluginConfigEntry
from src.events.payment_events import PaymentCapturedEvent


@pytest.fixture
def mock_stripe(mocker):
    """Mock stripe module."""
    mock_mod = mocker.MagicMock()
    mock_mod.error.SignatureVerificationError = type(
        "SignatureVerificationError", (Exception,), {}
    )
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
    container.event_dispatcher.return_value = mocker.MagicMock()
    return container


@pytest.fixture
def app(mock_stripe, mock_config_store, mock_container, mocker):
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True

    # Mock auth machinery for require_auth-decorated routes
    mock_auth_service = MagicMock()
    mock_auth_service.return_value.verify_token.return_value = str(uuid4())
    mocker.patch("src.middleware.auth.AuthService", mock_auth_service)

    mock_user = MagicMock()
    mock_user.status.value = "ACTIVE"
    mock_user_repo = MagicMock()
    mock_user_repo.return_value.find_by_id.return_value = mock_user
    mocker.patch("src.middleware.auth.UserRepository", mock_user_repo)
    mocker.patch("src.middleware.auth.db", MagicMock())

    from plugins.stripe.routes import stripe_plugin_bp

    flask_app.register_blueprint(stripe_plugin_bp, url_prefix="/api/v1/plugins/stripe")
    flask_app.config_store = mock_config_store
    flask_app.container = mock_container
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def invoice_id():
    return str(uuid4())


@pytest.fixture
def checkout_event(invoice_id):
    """A checkout.session.completed Stripe event object."""
    return {
        "id": "cs_test_abc",
        "metadata": {"invoice_id": invoice_id, "user_id": str(uuid4())},
        "amount_total": 2999,
        "currency": "eur",
        "payment_intent": "pi_test_xyz",
        "subscription": None,
    }


class TestWebhookEmitsPaymentCapturedEvent:
    """Verify webhook route emits PaymentCapturedEvent correctly."""

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

    def test_emits_payment_captured_event(
        self, client, mock_stripe, mock_container, checkout_event
    ):
        """Webhook should call dispatcher.emit with a PaymentCapturedEvent."""
        self._post_webhook(client, mock_stripe, "checkout.session.completed", checkout_event)

        dispatcher = mock_container.event_dispatcher.return_value
        dispatcher.emit.assert_called_once()
        event = dispatcher.emit.call_args[0][0]
        assert isinstance(event, PaymentCapturedEvent)

    def test_event_correct_invoice_id(
        self, client, mock_stripe, mock_container, checkout_event, invoice_id
    ):
        """Emitted event should have the correct invoice_id."""
        self._post_webhook(client, mock_stripe, "checkout.session.completed", checkout_event)

        event = mock_container.event_dispatcher.return_value.emit.call_args[0][0]
        assert str(event.invoice_id) == invoice_id

    def test_event_correct_amount(
        self, client, mock_stripe, mock_container, checkout_event
    ):
        """Emitted event amount should be amount_total / 100 as string."""
        self._post_webhook(client, mock_stripe, "checkout.session.completed", checkout_event)

        event = mock_container.event_dispatcher.return_value.emit.call_args[0][0]
        assert event.amount == str(2999 / 100)

    def test_event_correct_provider(
        self, client, mock_stripe, mock_container, checkout_event
    ):
        """Emitted event provider should be 'stripe'."""
        self._post_webhook(client, mock_stripe, "checkout.session.completed", checkout_event)

        event = mock_container.event_dispatcher.return_value.emit.call_args[0][0]
        assert event.provider == "stripe"

    def test_event_correct_transaction_id(
        self, client, mock_stripe, mock_container, checkout_event
    ):
        """Emitted event transaction_id should match payment_intent."""
        self._post_webhook(client, mock_stripe, "checkout.session.completed", checkout_event)

        event = mock_container.event_dispatcher.return_value.emit.call_args[0][0]
        assert event.transaction_id == "pi_test_xyz"

    def test_webhook_no_direct_domain_mutations(
        self, client, mock_stripe, mock_container, checkout_event
    ):
        """Webhook should NOT call save/update on invoice or subscription repos."""
        self._post_webhook(client, mock_stripe, "checkout.session.completed", checkout_event)

        invoice_repo = mock_container.invoice_repository.return_value
        sub_repo = mock_container.subscription_repository.return_value

        invoice_repo.save.assert_not_called()
        invoice_repo.update.assert_not_called()
        sub_repo.save.assert_not_called()
        sub_repo.update.assert_not_called()
