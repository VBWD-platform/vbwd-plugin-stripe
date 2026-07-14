"""Guard: Stripe is never asked for mode=subscription with empty line_items.

Reproduces the production trial-checkout failure. The payment mode-check
(``is_recurring_line_item``) and the Stripe line-item builder
(``recurring_billing_spec``) use different registry methods and swallow handler
exceptions, so they can diverge: a plan with no billing period is flagged
recurring (``mode=subscription``) while the spec lookup raises on ``None.value``
and is swallowed, yielding zero line_items. Stripe then rejects the request:

    Subscriptions require at least one recurring price or plan ... to line_items.

``create_session`` must never send that impossible request — when the
subscription line items come out empty it falls back to one-time payment mode.

This test is self-contained (its own Flask app + diverging registry) so the
plugin gate's ``tests/unit/`` scope exercises the guard directly.
"""
import sys
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from flask import Flask

from vbwd.models.enums import InvoiceStatus, LineItemType
from vbwd.plugins.config_store import PluginConfigEntry

USER_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


@pytest.fixture
def mock_stripe(mocker):
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
    container.user_repository.return_value = mocker.MagicMock()
    return container


@pytest.fixture
def diverging_registry():
    """Handler flags a line recurring but its billing-spec lookup raises —
    the exact swallowed-exception path (a spec-less plan → ``None.value``)."""
    from vbwd.events.line_item_registry import (
        line_item_registry,
        ILineItemHandler,
        LineItemResult,
    )

    class _DivergingHandler(ILineItemHandler):
        def can_handle_line_item(self, line_item, context):
            return True

        def activate_line_item(self, line_item, context):
            return LineItemResult.skip()

        def reverse_line_item(self, line_item, context):
            return LineItemResult.skip()

        def restore_line_item(self, line_item, context):
            return LineItemResult.skip()

        def is_recurring_line_item(self, line_item):
            return getattr(line_item, "_diverges", False)

        def recurring_billing_spec(self, line_item):
            if getattr(line_item, "_diverges", False):
                raise AttributeError("'NoneType' object has no attribute 'value'")
            return None

    saved = line_item_registry.handlers
    line_item_registry.clear()
    line_item_registry.register(_DivergingHandler())
    yield line_item_registry
    line_item_registry.clear()
    for handler in saved:
        line_item_registry.register(handler)


@pytest.fixture
def app(mock_stripe, mock_config_store, mock_container, mocker):
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True

    mock_auth_service = MagicMock()
    mock_auth_service.return_value.verify_token.return_value = str(USER_ID)
    mocker.patch("vbwd.middleware.auth.AuthService", mock_auth_service)

    mock_user = MagicMock()
    mock_user.id = USER_ID
    mock_user.status.value = "ACTIVE"
    mock_user_repo = MagicMock()
    mock_user_repo.return_value.find_by_id.return_value = mock_user
    mocker.patch("vbwd.middleware.auth.UserRepository", mock_user_repo)
    mocker.patch("vbwd.middleware.auth.db", MagicMock())

    from plugins.stripe.stripe.routes import stripe_plugin_bp

    flask_app.register_blueprint(stripe_plugin_bp, url_prefix="/api/v1/plugins/stripe")
    flask_app.config_store = mock_config_store
    flask_app.container = mock_container
    return flask_app


def _make_diverging_invoice():
    line_item = MagicMock()
    line_item.item_type = LineItemType.SUBSCRIPTION
    line_item.item_id = uuid4()
    line_item.unit_price = Decimal("9.99")
    line_item.quantity = 1
    line_item._diverges = True

    invoice = MagicMock()
    invoice.id = uuid4()
    invoice.status = InvoiceStatus.PENDING
    invoice.user_id = USER_ID
    invoice.total_amount = Decimal("9.99")
    invoice.amount = Decimal("9.99")
    invoice.currency = "EUR"
    invoice.line_items = [line_item]
    return invoice


def test_empty_subscription_items_never_sent_to_stripe(
    app, mock_stripe, mock_container, diverging_registry
):
    from vbwd.plugins.payment_route_helpers import determine_session_mode
    from plugins.stripe.stripe.routes import _build_stripe_subscription_items

    invoice = _make_diverging_invoice()

    # Precondition — the exact production divergence.
    assert determine_session_mode(invoice) == "subscription"
    assert _build_stripe_subscription_items(invoice) == []

    mock_container.invoice_repository.return_value.find_by_id.return_value = invoice
    user = MagicMock()
    user.email = "test@example.com"
    user.payment_customer_id = "cus_existing"
    mock_container.user_repository.return_value.find_by_id.return_value = user

    session = MagicMock()
    session.id = "cs_fallback"
    session.url = "https://checkout.stripe.com/cs_fallback"
    mock_stripe.checkout.Session.create.return_value = session

    resp = app.test_client().post(
        "/api/v1/plugins/stripe/create-session",
        json={"invoice_id": str(invoice.id)},
        headers={"Authorization": "Bearer test_token"},
    )

    assert resp.status_code == 200
    for call in mock_stripe.checkout.Session.create.call_args_list:
        if call.kwargs.get("mode") == "subscription":
            assert call.kwargs.get("line_items"), (
                "Stripe subscription session created with empty line_items — "
                "the production trial-checkout failure"
            )
