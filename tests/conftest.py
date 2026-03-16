"""Shared fixtures for Stripe plugin tests."""
import sys
import pytest
from unittest.mock import MagicMock

from src.sdk.interface import SDKConfig
from src.plugins.config_store import PluginConfigEntry


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
