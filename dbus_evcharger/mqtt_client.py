"""Nonblocking MQTT snapshots with independent field freshness."""

import logging
import math
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_FIELDS = (
    "status",
    "power",
    "current",
    "energy_forward",
    "session_time",
    "startstop",
    "setcurrent",
)
_STATUSES = {
    "disconnected",
    "connected",
    "charging",
    "charged",
    "waiting_for_sun",
    "waiting_for_rfid",
    "waiting_for_start",
    "low_soc",
    "ground_test_error",
    "welded_contacts_error",
    "cp_input_test_error",
    "residual_current",
    "undervoltage",
    "overvoltage",
    "overheating",
}
_FRESH_SECONDS = 5.0


class MqttClient:
    """Keep one reconnecting client and expose only fresh received fields.

    A usable snapshot requires both status and power within five seconds. Optional
    topic traffic cannot renew them. Broker connectivity alone is not telemetry.
    """

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
        self.topic = topic.rstrip("/")
        self.qos = qos
        self._connected = False
        self._snapshot: dict[str, Any] = dict.fromkeys(_FIELDS)
        self._received_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._client: Any = None
        self._started = False
        self._closed = False
        self._retry_after = 0.0

    @property
    def _configured(self) -> bool:
        return bool(self.host)

    def poll(self, timeout: float = 1.0) -> dict[str, Any]:
        """Return immediately; the paho loop owns network work and reconnects.

        ``timeout`` remains accepted for compatibility with the HA poll contract.
        Expired/invalid fields are None; a received finite zero remains zero.
        """
        if self._configured and time.monotonic() >= self._retry_after:
            try:
                self._connect()
            except (OSError, ConnectionError, ImportError, ValueError) as exc:
                logger.debug("MQTT connection setup failed: %s", exc)
                self._retry_after = time.monotonic() + _FRESH_SECONDS

        with self._lock:
            now = time.monotonic()
            result = {
                field: (
                    self._snapshot[field]
                    if self._connected
                    and not self._closed
                    and field in self._received_at
                    and now - self._received_at[field] < _FRESH_SECONDS
                    else None
                )
                for field in _FIELDS
            }
            result["ok"] = result["status"] is not None and result["power"] is not None
            # Preserve deadlines when another worker/main-loop stage queues this snapshot.
            result["_expires_at"] = {
                field: stamp + _FRESH_SECONDS for field, stamp in self._received_at.items()
            }
            return result

    def _connect(self) -> None:
        """Start one background loop; repeated polls never create another client."""
        with self._lifecycle_lock:
            if self._closed or self._started:
                return
            if self._client is None:
                # Keep optional paho imports off unconfigured/native-only paths.
                import paho.mqtt.client as mqtt

                self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
                if self.username:
                    self._client.username_pw_set(self.username, self.password)
                self._client.reconnect_delay_set(min_delay=1, max_delay=60)
                self._client.on_message = self._on_message
                self._client.on_connect = self._on_connect
                self._client.on_disconnect = self._on_disconnect
            # connect_async only records the target; DNS/TCP happens on paho's loop.
            self._client.connect_async(self.host, self.port, keepalive=60)
            self._client.loop_start()
            self._started = True

    def _on_message(self, _client: Any, _userdata: Any, msg: Any) -> None:
        parts = msg.topic.rsplit("/", 1)
        if len(parts) != 2 or parts[0] != self.topic:
            return
        with self._lock:
            if not self._connected or self._closed:
                return
        try:
            payload = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            payload = ""  # Malformed bytes invalidate this field, not unrelated ones.
        self._update_field(parts[1], payload)

    def _on_connect(
        self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._connected = reason_code == 0
            self._received_at.clear()
        if reason_code != 0:
            logger.warning("MQTT connection refused: %s", reason_code)
            return
        logger.info("MQTT connected to %s:%s", self.host, self.port)
        for field in _FIELDS:
            self._subscribe(f"{self.topic}/{field}")

    def _on_disconnect(
        self,
        _client: Any,
        _userdata: Any,
        _flags_or_rc: Any,
        _reason_code: Any = None,
        _properties: Any = None,
    ) -> None:
        with self._lock:
            self._connected = False
            self._received_at.clear()
        logger.info("MQTT disconnected")

    def _subscribe(self, topic: str) -> None:
        if self._client is not None and not self._closed:
            self._client.subscribe(topic, qos=self.qos)

    def _update_field(self, field: str, payload: str) -> None:
        """Parse only supported values; invalid payloads immediately expire a field."""
        if field not in _FIELDS:
            return
        try:
            if field == "status":
                value: Any = payload.strip().lower().replace(" ", "_")
                if value not in _STATUSES:
                    raise ValueError("unknown charger status")
            else:
                value = float(payload)
                if not math.isfinite(value):
                    raise ValueError("nonfinite charger value")
                if field in ("startstop", "session_time"):
                    value = int(value)
        except (ValueError, TypeError, OverflowError):
            logger.debug("Invalid MQTT payload for %s", field)
            with self._lock:
                self._snapshot[field] = None
                self._received_at.pop(field, None)
            return
        with self._lock:
            if self._closed:
                return
            self._snapshot[field] = value
            self._received_at[field] = time.monotonic()

    def disconnect(self) -> None:
        """Stop even a connecting/retrying loop; later polls cannot restart it."""
        with self._lifecycle_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                self._connected = False
                self._received_at.clear()
            if self._client is not None:
                # Stop outside the cache lock: a final callback may need that lock.
                try:
                    self._client.disconnect()
                finally:
                    self._client.loop_stop()
                    self._started = False
