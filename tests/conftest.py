"""Shared fixtures for Stripe plugin tests."""
import sys
from unittest.mock import MagicMock

import pytest

from vbwd.sdk.interface import SDKConfig
from vbwd.plugins.config_store import PluginConfigEntry


@pytest.fixture
def fake_lifecycle():
    """Register a spy ISubscriptionLifecycle (the webhook write port).

    Stripe webhooks delegate recurring link/renew/cancel/fail to this port; the
    test asserts the right port call instead of the old subscription-repo seam.
    """
    from vbwd.services.subscription_lifecycle import (
        ISubscriptionLifecycle,
        register_subscription_lifecycle,
        clear_subscription_lifecycle,
    )

    lifecycle = MagicMock(spec=ISubscriptionLifecycle)
    register_subscription_lifecycle(lifecycle)
    yield lifecycle
    clear_subscription_lifecycle()


@pytest.fixture
def recurring_registry():
    """Line-item registry carrying a fake handler that reports a line item as
    recurring iff the test attached a ``_recurring_spec`` to it.

    This is the seam ``determine_session_mode`` /
    ``_build_stripe_subscription_items`` now use (recurrence owned by the
    registry, not the payment plugin). Saves + restores the singleton's
    handlers so the global state is untouched after the test.
    """
    from vbwd.events.line_item_registry import (
        line_item_registry,
        ILineItemHandler,
        LineItemResult,
    )

    class _FakeRecurringHandler(ILineItemHandler):
        def can_handle_line_item(self, line_item, context):
            return True

        def activate_line_item(self, line_item, context):
            return LineItemResult.skip()

        def reverse_line_item(self, line_item, context):
            return LineItemResult.skip()

        def restore_line_item(self, line_item, context):
            return LineItemResult.skip()

        def is_recurring_line_item(self, line_item):
            return getattr(line_item, "_recurring_spec", None) is not None

        def recurring_billing_spec(self, line_item):
            return getattr(line_item, "_recurring_spec", None)

    saved = line_item_registry.handlers
    line_item_registry.clear()
    line_item_registry.register(_FakeRecurringHandler())
    yield line_item_registry
    line_item_registry.clear()
    for handler in saved:
        line_item_registry.register(handler)


@pytest.fixture
def stripe_config():
    """Stripe plugin configuration dict."""
    return {
        "test_publishable_key": "pk_test_abc123",
        "test_secret_key": "sk_test_secret456",
        "test_webhook_secret": "whsec_test789",
        "sandbox": True,
    }


@pytest.fixture
def sdk_config(stripe_config):
    """SDKConfig instance built from stripe_config."""
    return SDKConfig(
        api_key=stripe_config["test_secret_key"],
        sandbox=stripe_config["sandbox"],
    )


@pytest.fixture
def mock_stripe(mocker):
    """Mock the stripe module and inject it into sys.modules.

    Returns the mock stripe module so tests can configure it.
    """
    mock_mod = mocker.MagicMock()
    mocker.patch.dict(sys.modules, {"stripe": mock_mod})
    return mock_mod


@pytest.fixture
def mock_config_store(mocker, stripe_config):
    """Mock PluginConfigStore with enabled Stripe entry.

    Returns the mock so tests can reconfigure it.
    """
    store = mocker.MagicMock()
    store.get_by_name.return_value = PluginConfigEntry(
        plugin_name="stripe",
        status="enabled",
        config=stripe_config,
    )
    store.get_config.return_value = stripe_config
    return store
