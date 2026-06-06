"""Shared fixtures for Stripe plugin tests."""
import sys

import pytest

from vbwd.sdk.interface import SDKConfig
from vbwd.plugins.config_store import PluginConfigEntry


@pytest.fixture
def published_events():
    """Capture domain-neutral recurring-billing facts published to the bus.

    S50.4: stripe webhooks no longer call a subscription write port — they
    *publish* generic facts (payment.provider_linked / recurring_charge /
    provider_cancelled / recurring_failed / invoice_failed) and the subscription
    plugin (if enabled) subscribes. The test subscribes a spy and asserts the
    right event name + domain-neutral payload, never a subscription symbol.
    """
    from vbwd.events.bus import event_bus
    from vbwd.plugins.payment_route_helpers import (
        EVENT_PROVIDER_LINKED,
        EVENT_RECURRING_CHARGE,
        EVENT_PROVIDER_CANCELLED,
        EVENT_RECURRING_FAILED,
        EVENT_INVOICE_FAILED,
    )

    captured: list[tuple[str, dict]] = []

    def _spy(event_name, data):
        captured.append((event_name, data))

    names = [
        EVENT_PROVIDER_LINKED,
        EVENT_RECURRING_CHARGE,
        EVENT_PROVIDER_CANCELLED,
        EVENT_RECURRING_FAILED,
        EVENT_INVOICE_FAILED,
    ]
    for name in names:
        event_bus.subscribe(name, _spy)
    yield captured
    for name in names:
        event_bus.unsubscribe(name, _spy)


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
