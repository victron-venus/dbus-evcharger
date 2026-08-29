# Copy to local_config.py and fill in real values. NEVER commit local_config.py.

# Home Assistant (optional - primary data source)
HA_URL = "http://192.168.1.50:8123"
HA_TOKEN = "your_long_lived_access_token_here"

# HA entities for EV charger (required if using HA)
HA_STATUS_ENTITY = "sensor.evcharger_status"
HA_POWER_ENTITY = "sensor.evcharger_power"
HA_CURRENT_ENTITY = "sensor.evcharger_current"
HA_ENERGY_ENTITY = "sensor.evcharger_energy_total"
HA_SESSION_TIME_ENTITY = "sensor.evcharger_session_time"
HA_STARTSTOP_ENTITY = "switch.evcharger_startstop"
HA_SETCURRENT_ENTITY = "number.evcharger_setcurrent"

# MQTT (optional - fallback data source)
MQTT_ENABLED = False
MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_USERNAME = ""
MQTT_PASSWORD = ""
MQTT_TOPIC = "evcharger"
MQTT_QOS = 1

# D-Bus instance
DEVICE_INSTANCE = 40  # com.victronenergy.evcharger.40

# Device defaults
DEFAULT_POSITION = 0  # 0=AC Output, 1=AC Input
DEFAULT_MAX_CURRENT = 32.0  # A
DEFAULT_MIN_CURRENT = 6.0  # A
DEFAULT_NR_OF_PHASES = 1
DEFAULT_CUSTOM_NAME = "EV Charger"