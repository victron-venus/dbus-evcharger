"""Tests for config module."""

import builtins
from unittest.mock import MagicMock

from dbus_evcharger import config


def test_ha_configured_true():
    """HA configured when URL and token are both non-empty."""
    result = config.ha_configured()
    assert isinstance(result, bool)


def test_mqtt_configured_false_by_default():
    """MQTT configured when MQTT_ENABLED and host are set."""
    result = config.mqtt_configured()
    assert isinstance(result, bool)


def test_read_version_returns_string():
    """_read_version returns a non-empty string."""
    v = config._read_version()
    assert isinstance(v, str)
    assert len(v) > 0


def test_get_with_missing_local_config(monkeypatch):
    """_get falls back to default when local_config missing."""
    monkeypatch.setattr(config, "local_config", None)
    result = config._get("SOME_VAR_XYZ", "default_val")
    assert result == "default_val"


def test_get_with_local_config_present(monkeypatch):
    """_get returns local_config value when present."""
    fake_config = MagicMock()
    fake_config.MY_VAR = "from_local"
    monkeypatch.setattr(config, "local_config", fake_config)
    result = config._get("MY_VAR", "fallback")
    assert result == "from_local"


def test_get_missing_key_falls_back_to_default(monkeypatch):
    """_get returns default when key not in local_config."""

    # Use a real dict-like object so hasattr works correctly
    class FakeConfig:
        OTHER_VAR = "other"

    monkeypatch.setattr(config, "local_config", FakeConfig())
    result = config._get("MISSING_VAR", "default")
    assert result == "default"


def test_read_version_file_not_found(monkeypatch):
    """_read_version returns default version on OSError."""
    real_open = builtins.open

    def raising_open(*args, **kwargs):
        if "version" in str(args[0]):
            raise OSError("no such file")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", raising_open)
    result = config._read_version()
    assert result == "0.1.0"


def test_config_module_has_all_expected_attrs():
    """Sanity check: key config values exist."""
    attrs = [
        "DEVICE_INSTANCE",
        "PRODUCT_NAME",
        "SOFTWARE_VERSION",
        "HA_URL",
        "HA_TOKEN",
        "HA_STATUS_ENTITY",
        "HA_POWER_ENTITY",
        "HA_CURRENT_ENTITY",
        "HA_ENERGY_ENTITY",
        "HA_SESSION_TIME_ENTITY",
        "HA_STARTSTOP_ENTITY",
        "HA_SETCURRENT_ENTITY",
        "MQTT_HOST",
        "MQTT_PORT",
        "MQTT_USERNAME",
        "MQTT_PASSWORD",
        "MQTT_TOPIC",
        "MQTT_QOS",
        "POLL_INTERVAL",
        "HA_TIMEOUT",
        "HEARTBEAT_FILE",
        "DEFAULT_POSITION",
        "DEFAULT_MAX_CURRENT",
        "DEFAULT_MIN_CURRENT",
        "DEFAULT_NR_OF_PHASES",
        "DEFAULT_CUSTOM_NAME",
        "ha_configured",
        "mqtt_configured",
    ]
    for attr in attrs:
        assert hasattr(config, attr), f"missing {attr}"


def test_mqtt_enabled_type():
    """MQTT_ENABLED is a bool."""
    assert isinstance(config.MQTT_ENABLED, bool)


def test_poll_interval_type():
    """POLL_INTERVAL is a float."""
    assert isinstance(config.POLL_INTERVAL, float)


def test_poll_interval_default_is_15s():
    """Default POLL_INTERVAL is 15.0 seconds when local_config missing."""
    import importlib

    importlib.reload(config)
    assert config.POLL_INTERVAL == 15.0


def test_heartbeat_file_is_string():
    """HEARTBEAT_FILE is a string path."""
    assert isinstance(config.HEARTBEAT_FILE, str)
    assert config.HEARTBEAT_FILE.startswith("/")


def test_default_values_types():
    """Default device values have correct types."""
    assert isinstance(config.DEVICE_INSTANCE, int)
    assert isinstance(config.DEFAULT_POSITION, int)
    assert isinstance(config.DEFAULT_MAX_CURRENT, float)
    assert isinstance(config.DEFAULT_MIN_CURRENT, float)
    assert isinstance(config.DEFAULT_NR_OF_PHASES, int)
    assert isinstance(config.DEFAULT_CUSTOM_NAME, str)
    assert isinstance(config.MQTT_PORT, int)
    assert isinstance(config.MQTT_QOS, int)
    assert isinstance(config.POLL_INTERVAL, float)
    assert isinstance(config.HA_TIMEOUT, float)
