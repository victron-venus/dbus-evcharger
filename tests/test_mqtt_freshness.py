"""MQTT outage, field freshness and loop-lifecycle regression cases."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dbus_evcharger import mqtt_client as module
from dbus_evcharger.mqtt_client import MqttClient


@pytest.fixture(name="mqtt_case")
def make_mqtt_case(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    wire = MagicMock()
    constructor = MagicMock(return_value=wire)
    monkeypatch.setattr("paho.mqtt.client.Client", constructor)
    client = MqttClient("broker", username="test", password="test", topic="charger/", qos=2)
    yield client, wire, clock, constructor
    client.disconnect()


def receive(wire, field, value):
    wire.on_message(wire, None, SimpleNamespace(topic=f"charger/{field}", payload=value.encode()))


def connected(mqtt_case):
    client, wire, clock, constructor = mqtt_case
    assert client.poll()["ok"] is False
    wire.on_connect(wire, None, {}, 0, None)
    return client, wire, clock, constructor


def test_unacknowledged_connection_does_not_create_clients_or_loops(mqtt_case):
    client, wire, _, constructor = mqtt_case
    for _ in range(50):
        assert client.poll()["ok"] is False
    constructor.assert_called_once()
    wire.connect_async.assert_called_once_with("broker", 1883, keepalive=60)
    wire.loop_start.assert_called_once()
    wire.connect.assert_not_called()
    wire.reconnect.assert_not_called()
    wire.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=60)
    wire.username_pw_set.assert_called_once_with("test", "test")


def test_refused_connection_is_unavailable_and_does_not_subscribe(mqtt_case):
    client, wire, _, constructor = mqtt_case
    client.poll()
    wire.on_connect(wire, None, {}, 5, None)
    for _ in range(10):
        assert client.poll()["ok"] is False
    wire.subscribe.assert_not_called()
    constructor.assert_called_once()
    wire.loop_start.assert_called_once()


def test_reconnect_resubscribes_but_requires_new_required_fields(mqtt_case):
    client, wire, _, constructor = connected(mqtt_case)
    receive(wire, "status", "charging")
    receive(wire, "power", "0")
    assert client.poll()["ok"] is True
    wire.on_disconnect(wire, None, {}, 7, None)
    assert client.poll()["power"] is None
    wire.on_connect(wire, None, {}, 0, None)
    assert client.poll()["ok"] is False
    receive(wire, "power", "0")
    assert client.poll()["ok"] is False
    receive(wire, "status", "connected")
    assert client.poll()["ok"] is True
    constructor.assert_called_once()
    wire.loop_start.assert_called_once()
    assert wire.subscribe.call_count == 14
    wire.subscribe.assert_any_call("charger/status", qos=2)


@pytest.mark.parametrize("expired,refreshed", [("power", "status"), ("status", "power")])
def test_unrelated_topic_cannot_renew_required_field(mqtt_case, expired, refreshed):
    client, wire, clock, _ = connected(mqtt_case)
    receive(wire, "status", "charging")
    receive(wire, "power", "2500")
    assert client.poll()["ok"] is True  # monotonic zero is a valid receipt time
    clock[0] = 4.0
    receive(wire, refreshed, "charging" if refreshed == "status" else "2600")
    receive(wire, "current", "11")
    clock[0] = 4.999
    assert client.poll()["ok"] is True
    clock[0] = 5.0
    snap = client.poll()
    assert snap["ok"] is False
    assert snap[expired] is None
    assert snap[refreshed] is not None
    assert snap["current"] == 11


def test_optional_fields_expire_without_invalidating_fresh_required_fields(mqtt_case):
    client, wire, clock, _ = connected(mqtt_case)
    receive(wire, "energy_forward", "7")
    clock[0] = 5.0
    receive(wire, "status", "Waiting for sun")
    receive(wire, "power", "0")
    snap = client.poll()
    assert snap["ok"] is True
    assert snap["status"] == "waiting_for_sun"
    assert snap["power"] == 0.0
    assert snap["energy_forward"] is None
    assert snap["session_time"] is None


@pytest.mark.parametrize("payload", ["unknown", "", "NaN", "inf", "-inf", "1e1000"])
def test_invalid_power_clears_previous_value_and_can_recover(mqtt_case, payload):
    client, wire, _, _ = connected(mqtt_case)
    receive(wire, "status", "charging")
    receive(wire, "power", "4000")
    receive(wire, "power", payload)
    assert client.poll()["ok"] is False
    assert client.poll()["power"] is None
    receive(wire, "power", "0")
    assert client.poll()["ok"] is True


@pytest.mark.parametrize("payload", ["", "not_a_charger_state", "2", "nan"])
def test_invalid_status_does_not_appear_connected(mqtt_case, payload):
    client, wire, _, _ = connected(mqtt_case)
    receive(wire, "status", "charging")
    receive(wire, "power", "1")
    receive(wire, "status", payload)
    assert client.poll()["ok"] is False
    assert client.poll()["status"] is None
    assert client.poll()["power"] == 1


def test_malformed_bytes_invalidate_only_matching_field(mqtt_case):
    client, wire, _, _ = connected(mqtt_case)
    receive(wire, "status", "connected")
    receive(wire, "power", "5")
    wire.on_message(wire, None, SimpleNamespace(topic="charger/power", payload=b"\xff"))
    assert client.poll()["power"] is None
    assert client.poll()["status"] == "connected"


@pytest.mark.parametrize("field", ["session_time", "startstop"])
def test_nonfinite_integer_field_is_invalid(mqtt_case, field):
    client, wire, _, _ = connected(mqtt_case)
    receive(wire, field, "1")
    receive(wire, field, "inf")
    assert client.poll()[field] is None


def test_shutdown_stops_retrying_loop_and_rejects_late_callbacks(mqtt_case):
    client, wire, _, constructor = mqtt_case
    client.poll()  # Broker has never acknowledged the connection.
    client.disconnect()
    wire.disconnect.assert_called_once()
    wire.loop_stop.assert_called_once()
    wire.on_connect(wire, None, {}, 0, None)
    receive(wire, "status", "charging")
    receive(wire, "power", "100")
    for _ in range(10):
        assert client.poll()["ok"] is False
    client.disconnect()
    constructor.assert_called_once()
    wire.loop_start.assert_called_once()
    wire.loop_stop.assert_called_once()
    wire.subscribe.assert_not_called()


def test_setup_errors_are_throttled_and_reuse_existing_client(mqtt_case):
    client, wire, clock, constructor = mqtt_case
    wire.connect_async.side_effect = OSError("temporary setup failure")
    for _ in range(10):
        assert client.poll()["ok"] is False
    wire.connect_async.assert_called_once()
    wire.loop_start.assert_not_called()
    clock[0] = 5.0
    wire.connect_async.side_effect = None
    client.poll()
    assert wire.connect_async.call_count == 2
    wire.loop_start.assert_called_once()
    constructor.assert_called_once()


def test_shutdown_stops_loop_even_if_disconnect_raises(mqtt_case):
    client, wire, _, _ = mqtt_case
    client.poll()
    wire.disconnect.side_effect = OSError("transport failed during close")
    with pytest.raises(OSError):
        client.disconnect()
    wire.loop_stop.assert_called_once()
    assert client.poll()["ok"] is False
