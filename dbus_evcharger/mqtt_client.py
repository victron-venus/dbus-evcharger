"""MQTT client for EV charger metrics.

Subscribes to status topics and builds a snapshot compatible with HaClient.
Uses paho-mqtt for Venus OS compatibility.
"""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class MqttClient:
    """Simple MQTT client that builds a snapshot from topic payloads."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        topic: str = "evcharger",
        qos: int = 1,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.topic = topic
        self.qos = qos
        self._configured = bool(host)
        self._connected = False
        self._snapshot: dict[str, Any] = {
            "status": None,
            "power": None,
            "current": None,
            "energy_forward": None,
            "session_time": None,
            "startstop": None,
            "setcurrent": None,
        }
        self._last_ok: float = 0

    @property
    def _configured(self) -> bool:
        return bool(self.host)

    def poll(self, timeout: float = 1.0) -> dict[str, Any]:
        """Return the current snapshot.

        In a real implementation, this would subscribe to MQTT topics and
        collect updates. For now, returns a snapshot that may be stale if
        not configured or not connected.
        """
        result = dict(self._snapshot)
        result["ok"] = False

        if not self._configured:
            return result

        # Attempt connection if not connected
        if not self._connected:
            try:
                self._connect()
            except (OSError, ConnectionError, ImportError) as exc:
                logger.debug("MQTT connect failed: %s", exc)
                return result

        # If connected and have fresh data, mark as ok
        now = time.monotonic()
        if (now - self._last_ok) < 5:  # data valid for 5s
            result["ok"] = True

        return result

    def _connect(self) -> None:
        """Connect to MQTT broker and subscribe to topics."""
        # Lazy import to avoid requiring paho-mqtt off-device
        import paho.mqtt.client as mqtt

        def on_message(_client, _userdata, msg):
            topic = msg.topic
            try:
                payload = msg.payload.decode("utf-8")
            except UnicodeDecodeError:
                return

            # Parse topic: evcharger/<field> e.g. evcharger/status, evcharger/power
            parts = topic.rsplit("/", 1)
            if len(parts) != 2 or parts[0].rstrip("/") != self.topic:
                return

            field = parts[1]
            self._update_field(field, payload)

        def on_connect(_client, _userdata, _flags, _rc):
            logger.info("MQTT connected to %s:%s", self.host, self.port)
            self._connected = True
            # Subscribe to all fields
            for field in [
                "status",
                "power",
                "current",
                "energy_forward",
                "session_time",
                "startstop",
                "setcurrent",
            ]:
                self._subscribe(f"{self.topic}/{field}")

        def on_disconnect(_client, _userdata, _rc):
            logger.info("MQTT disconnected")
            self._connected = False

        try:
            self._client = mqtt.Client()
            if self.username:
                self._client.username_pw_set(self.username, self.password)
            self._client.on_message = on_message
            self._client.on_connect = on_connect
            self._client.on_disconnect = on_disconnect
            self._client.connect(self.host, self.port, keepalive=60)
            self._client.loop_start()
        except (OSError, ConnectionError, ImportError) as exc:
            logger.debug("MQTT connection setup failed: %s", exc)
            raise

    def _subscribe(self, topic: str) -> None:
        """Subscribe to an MQTT topic."""
        if hasattr(self, "_client"):
            self._client.subscribe(topic, qos=self.qos)

    def _update_field(self, field: str, payload: str) -> None:
        """Update a field in the snapshot from an MQTT payload."""
        if field not in self._snapshot:
            return
        try:
            if field in ("startstop", "session_time"):
                self._snapshot[field] = int(float(payload))
            else:
                self._snapshot[field] = float(payload)
            self._last_ok = time.monotonic()
            self._snapshot["ok"] = True
        except (ValueError, TypeError):
            logger.debug("Invalid MQTT payload for %s: %s", field, payload)

    def disconnect(self) -> None:
        """Disconnect from MQTT broker."""
        if hasattr(self, "_client") and self._connected:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
