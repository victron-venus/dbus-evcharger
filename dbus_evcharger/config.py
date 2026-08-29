"""Configuration.

Real values live in local_config.py at the repo root (gitignored).
Falls back to safe defaults when it is missing.
"""

import logging
import os

logger = logging.getLogger(__name__)

try:
    import local_config  # type: ignore
except ImportError:
    local_config = None  # type: ignore
    logger.warning("local_config.py not found - using defaults/example values")


def _get(name: str, default):
    if local_config is not None and hasattr(local_config, name):
        return getattr(local_config, name)
    return default


def _read_version() -> str:
    try:
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "version")
        ) as f:
            return f.read().strip()
    except OSError:
        return "0.1.0"


# --- D-Bus identity ----------------------------------------------------------
DEVICE_INSTANCE: int = int(_get("DEVICE_INSTANCE", 40))
PRODUCT_NAME = "dbus-evcharger"
SOFTWARE_VERSION = _read_version()

# --- Home Assistant (optional) -----------------------------------------------
HA_URL: str = str(_get("HA_URL", "")).rstrip("/")
HA_TOKEN: str = str(_get("HA_TOKEN", ""))
HA_STATUS_ENTITY: str = str(_get("HA_STATUS_ENTITY", ""))
HA_POWER_ENTITY: str = str(_get("HA_POWER_ENTITY", ""))
HA_CURRENT_ENTITY: str = str(_get("HA_CURRENT_ENTITY", ""))
HA_ENERGY_ENTITY: str = str(_get("HA_ENERGY_ENTITY", ""))
HA_SESSION_TIME_ENTITY: str = str(_get("HA_SESSION_TIME_ENTITY", ""))
HA_STARTSTOP_ENTITY: str = str(_get("HA_STARTSTOP_ENTITY", ""))
HA_SETCURRENT_ENTITY: str = str(_get("HA_SETCURRENT_ENTITY", ""))

# --- MQTT (optional) ---------------------------------------------------------
MQTT_ENABLED: bool = bool(_get("MQTT_ENABLED", False))
MQTT_HOST: str = str(_get("MQTT_HOST", "localhost"))
MQTT_PORT: int = int(_get("MQTT_PORT", 1883))
MQTT_USERNAME: str = str(_get("MQTT_USERNAME", ""))
MQTT_PASSWORD: str = str(_get("MQTT_PASSWORD", ""))
MQTT_TOPIC: str = str(_get("MQTT_TOPIC", "evcharger"))
MQTT_QOS: int = int(_get("MQTT_QOS", 1))

# --- polling ----------------------------------------------------------------
POLL_INTERVAL: float = float(_get("POLL_INTERVAL", 2.0))  # s
HA_TIMEOUT: float = float(_get("HA_TIMEOUT", 3.0))
HEARTBEAT_FILE = "/run/dbus-evcharger/heartbeat"

# --- device defaults ---------------------------------------------------------
DEFAULT_POSITION: int = int(_get("DEFAULT_POSITION", 0))  # 0=AC Output, 1=AC Input
DEFAULT_MAX_CURRENT: float = float(_get("DEFAULT_MAX_CURRENT", 32.0))  # A
DEFAULT_MIN_CURRENT: float = float(_get("DEFAULT_MIN_CURRENT", 6.0))  # A
DEFAULT_NR_OF_PHASES: int = int(_get("DEFAULT_NR_OF_PHASES", 1))
DEFAULT_CUSTOM_NAME: str = str(_get("DEFAULT_CUSTOM_NAME", "EV Charger"))


def ha_configured() -> bool:
    """True when HA REST API is configured (base_url + token only)."""
    return bool(HA_URL and HA_TOKEN)


def mqtt_configured() -> bool:
    """True when MQTT is configured."""
    return MQTT_ENABLED and bool(MQTT_HOST)
