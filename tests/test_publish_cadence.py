"""Independent MQTT freshness checks must not accelerate blocking HA requests."""

# White-box timing tests avoid live D-Bus/network calls.
# pylint: disable=protected-access

from types import SimpleNamespace

from dbus_evcharger import main, mqtt_client
from dbus_evcharger.mqtt_client import MqttClient
from tests.test_app_tick import BASE, FakeClient, build_app


class DeferredWorker:
    """Record scheduled jobs and let the test deliver them on the main loop."""

    def __init__(self):
        self.callbacks = []

    def poll(self, callback):
        self.callbacks.append(callback)
        return True


def test_default_cadence_publishes_completion_immediately_without_extra_poll(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(main, "_now", lambda: clock[0])
    monkeypatch.setattr(main, "_write_heartbeat", lambda: None)
    monkeypatch.setattr(main.config, "POLL_INTERVAL", 15.0)
    app = build_app(BASE, mqtt_client=FakeClient({}, configured=False))
    worker = DeferredWorker()
    app._worker = worker
    assert app.loop_interval_ms == 1000
    app.tick()
    assert app.service.svc["/Connected"] == 0
    assert len(worker.callbacks) == 1
    clock[0] = 0.5
    worker.callbacks[0](dict(BASE, ok=True), 0.0)
    assert app.service.svc["/Connected"] == 1
    assert app.service.svc["/Ac/Power"] == BASE["power"]
    assert len(worker.callbacks) == 1
    for second in range(1, 15):
        clock[0] = second
        app.tick()
    assert len(worker.callbacks) == 1
    clock[0] = 15.0
    app.tick()
    assert len(worker.callbacks) == 2


def test_continuous_mqtt_updates_between_ha_polls_stay_fresh_then_expire(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(main, "_now", lambda: clock[0])
    monkeypatch.setattr(mqtt_client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(main, "_write_heartbeat", lambda: None)
    monkeypatch.setattr(main.config, "POLL_INTERVAL", 15.0)
    monkeypatch.setattr(main.config, "HA_TIMEOUT", 3.0)
    mqtt = MqttClient("unused.invalid")
    monkeypatch.setattr(mqtt, "_connect", lambda: None)
    mqtt._connected = True
    ha = FakeClient({"ok": False})
    app = build_app({}, ha_client=ha, mqtt_client=mqtt)
    worker = DeferredWorker()
    app._worker = worker
    latest = None
    for second in range(21):
        clock[0] = float(second)
        if second % 4 == 0:
            latest = 1000 + second
            mqtt._update_field("status", "charging")
            mqtt._update_field("power", str(latest))
        app.tick()
        assert app.service.svc["/Connected"] == 1
        assert app.service.svc["/Ac/Power"] == latest
    assert len(worker.callbacks) == 2  # Only configured 0s/15s HA/grid submissions.
    clock[0] = 24.0
    app.tick()
    assert app.service.svc["/Connected"] == 1
    clock[0] = 25.0  # Five seconds after the last genuine MQTT message.
    app.tick()
    assert app.service.svc["/Connected"] == 0
    assert app.service.svc["/Ac/Power"] is None
    assert len(worker.callbacks) == 2


def test_worker_collects_no_mqtt_and_expired_voltage_does_not_reappear(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(main, "_now", lambda: clock[0])
    monkeypatch.setattr(main, "_write_heartbeat", lambda: None)
    monkeypatch.setattr(main.config, "POLL_INTERVAL", 15.0)
    monkeypatch.setattr(main.config, "HA_TIMEOUT", 3.0)
    mqtt = FakeClient(dict(BASE, ok=True))
    calls = []
    mqtt.poll = lambda: calls.append(clock[0]) or dict(BASE, ok=True)
    app = build_app({}, ha_client=FakeClient({"ok": False}), mqtt_client=mqtt)
    app.voltage_reader = SimpleNamespace(read=lambda: (120.0, 121.0))
    app._worker = DeferredWorker()
    result = app._collect_snapshot()
    assert not calls
    app._accept_snapshot(result, 0.0)
    assert calls == [0.0]
    assert app.service.svc["/Ac/L1/Voltage"] == 120.0
    clock[0] = 30.0
    app.tick()
    assert app.service.svc["/Connected"] == 1
    assert app.service.svc["/Ac/L1/Voltage"] is None
    assert app.service.svc["/Ac/L1/Current"] is None
