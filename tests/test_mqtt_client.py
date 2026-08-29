"""Tests for MQTT client."""

from unittest.mock import MagicMock, patch

from dbus_evcharger.mqtt_client import MqttClient


class TestMqttClient:
    def test_not_configured_when_host_empty(self):
        c = MqttClient(host="")
        assert c._configured is False
        result = c.poll()
        assert result["ok"] is False

    def test_configured_when_host_set(self):
        c = MqttClient(host="mqtt.local")
        assert c._configured is True

    def test_unconfigured_returns_snapshot_ok_false(self):
        c = MqttClient(host="")
        result = c.poll()
        assert result["ok"] is False
        assert result["status"] is None
        assert result["power"] is None

    def test_poll_not_connected_connect_raises(self):
        c = MqttClient(host="mqtt.local")
        with patch("paho.mqtt.client.Client") as mock_client_cls:
            mock_client_cls.return_value.connect.side_effect = OSError("no broker")
            result = c.poll()
            assert result["ok"] is False

    def test_poll_connected_stale_data(self):
        import time

        c = MqttClient(host="mqtt.local")
        c._connected = True
        c._last_ok = time.monotonic() - 10
        result = c.poll()
        assert result["ok"] is False

    def test_poll_connected_fresh_data(self):
        import time

        c = MqttClient(host="mqtt.local")
        c._connected = True
        c._last_ok = time.monotonic()
        c._snapshot["ok"] = True
        result = c.poll()
        assert result["ok"] is True

    def test_update_field_startstop_int(self):
        c = MqttClient(host="mqtt.local")
        c._update_field("startstop", "1")
        assert c._snapshot["startstop"] == 1

    def test_update_field_session_time_int(self):
        c = MqttClient(host="mqtt.local")
        c._update_field("session_time", "3600")
        assert c._snapshot["session_time"] == 3600

    def test_update_field_power_float(self):
        c = MqttClient(host="mqtt.local")
        c._update_field("power", "3800.5")
        assert c._snapshot["power"] == 3800.5

    def test_update_field_invalid_payload(self):
        import logging

        c = MqttClient(host="mqtt.local")
        with patch.object(logging.getLogger("dbus_evcharger.mqtt_client"), "debug") as mock_log:
            c._update_field("power", "not_a_number")
            assert c._snapshot["power"] is None
            mock_log.assert_called_once()

    def test_update_field_unknown_field_no_crash(self):
        c = MqttClient(host="mqtt.local")
        c._update_field("unknown_field", "42")

    def test_update_field_sets_ok_true(self):
        c = MqttClient(host="mqtt.local")
        c._update_field("power", "3800")
        assert c._snapshot["ok"] is True

    def test_poll_connect_import_error(self):
        c = MqttClient(host="mqtt.local")
        with patch("paho.mqtt.client.Client") as mock_client_cls:
            mock_client_cls.return_value.connect.side_effect = ImportError("module not found")
            result = c.poll()
            assert result["ok"] is False

    def test_disconnect_not_connected(self):
        c = MqttClient(host="mqtt.local")
        c.disconnect()

    def test_disconnect_connected(self):
        c = MqttClient(host="mqtt.local")
        client_mock = MagicMock()
        c._client = client_mock
        c._connected = True
        c.disconnect()
        client_mock.loop_stop.assert_called_once()
        client_mock.disconnect.assert_called_once()
        assert c._connected is False

    def test_subscribe_no_client(self):
        c = MqttClient(host="mqtt.local")
        c._subscribe("evcharger/power")

    def test_on_message_valid(self):
        c = MqttClient(host="mqtt.local")
        # Use _connect to set real callbacks, then disconnect to prevent real I/O
        with patch("paho.mqtt.client.Client") as mock_client_cls:
            mock_client_cls.return_value.connect.return_value = None
            c._connect()
            on_message_cb = c._client.on_message
            msg = MagicMock()
            msg.topic = "evcharger/power"
            msg.payload = b"3800.0"
            on_message_cb(c._client, None, msg)
            assert c._snapshot["power"] == 3800.0

    def test_on_message_wrong_topic_prefix(self):
        c = MqttClient(host="mqtt.local")
        with patch("paho.mqtt.client.Client") as mock_client_cls:
            mock_client_cls.return_value.connect.return_value = None
            c._connect()
            on_message_cb = c._client.on_message
            msg = MagicMock()
            msg.topic = "wrong/power"
            msg.payload = b"3800.0"
            on_message_cb(c._client, None, msg)
            assert c._snapshot["power"] is None

    def test_on_message_unicode_decode_error(self):
        c = MqttClient(host="mqtt.local")
        with patch("paho.mqtt.client.Client") as mock_client_cls:
            mock_client_cls.return_value.connect.return_value = None
            c._connect()
            on_message_cb = c._client.on_message
            msg = MagicMock()
            msg.topic = "evcharger/power"
            msg.payload = b"\xff\xfe"
            on_message_cb(c._client, None, msg)

    def test_on_connect_sets_connected(self):
        c = MqttClient(host="mqtt.local")
        with patch("paho.mqtt.client.Client") as mock_client_cls:
            mock_client_cls.return_value.connect.return_value = None
            c._connect()
            on_connect_cb = c._client.on_connect
            on_connect_cb(c._client, None, None, 0)
            assert c._connected is True

    def test_on_disconnect_clears_connected(self):
        c = MqttClient(host="mqtt.local")
        with patch("paho.mqtt.client.Client") as mock_client_cls:
            mock_client_cls.return_value.connect.return_value = None
            c._connect()
            c._connected = True
            on_disconnect_cb = c._client.on_disconnect
            on_disconnect_cb(c._client, None, 0)
            assert c._connected is False

    def test_custom_topic(self):
        c = MqttClient(host="mqtt.local", topic="mycharger")
        assert c.topic == "mycharger"

    def test_custom_qos(self):
        c = MqttClient(host="mqtt.local", qos=2)
        assert c.qos == 2
