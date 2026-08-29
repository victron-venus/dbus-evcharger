"""Integration tests for App.tick with fake client/services."""

import dbus_evcharger.main as main_mod
from dbus_evcharger.ha_client import HaClient
from dbus_evcharger.service import EvChargerService


class FakeClient:
    def __init__(self, snapshot, configured=True):
        self.snapshot = snapshot
        self.calls = []
        self._configured = configured

    def poll(self):
        result = dict(self.snapshot)
        result.setdefault("ok", True)
        return result

    def call_service(self, domain, action, entity_id):
        self.calls.append((domain, action, entity_id))
        return True


def build_app(snapshot, ha_client=None, mqtt_client=None):
    service = EvChargerService(
        40,
        "0.1.0",
        custom_name="EV Charger",
        product_name="dbus-evcharger",
        connection="Local",
        on_mode=lambda p, m: None,
        on_startstop=lambda p, s: None,
        on_setcurrent=lambda p, c: None,
    )
    if ha_client is None:
        ha_client = FakeClient(snapshot)
    if mqtt_client is None:
        mqtt_client = FakeClient({})
    app = main_mod.App(ha_client, mqtt_client, service)
    return app


BASE = {"status": "charging", "power": 3800, "current": 16, "energy_forward": 12.5}


def test_tick_publishes_values(monkeypatch):
    monkeypatch.setattr(main_mod, "_write_heartbeat", lambda: None)
    app = build_app(dict(BASE))
    app.tick()
    assert app.service.svc["/Status"] == 2  # charging
    assert app.service.svc["/Ac/Power"] == 3800.0
    assert app.service.svc["/Current"] == 16.0
    assert app.service.svc["/Ac/Energy/Forward"] == 12.5


def test_tick_commands_startstop_from_snapshot(monkeypatch):
    monkeypatch.setattr(main_mod, "_write_heartbeat", lambda: None)
    snap = dict(BASE, startstop=1)
    app = build_app(snap)
    app.tick()
    assert app.service.svc["/StartStop"] == 1


def test_tick_commands_setcurrent_from_snapshot(monkeypatch):
    monkeypatch.setattr(main_mod, "_write_heartbeat", lambda: None)
    snap = dict(BASE, setcurrent=32.0)
    app = build_app(snap)
    app.tick()
    assert app.service.svc["/SetCurrent"] == 32.0


def test_tick_stale_marks_disconnected(monkeypatch):
    monkeypatch.setattr(main_mod, "_write_heartbeat", lambda: None)
    snap = {"status": "unknown", "ok": False}
    app = build_app(snap, mqtt_client=FakeClient({}, configured=False))
    app.tick()
    assert app.service.svc["/Status"] == 0  # disconnected
    assert app.service.svc["/Ac/Power"] is None
    assert app.service.svc["/Connected"] == 0


def test_tick_mqtt_fallback_when_ha_fails(monkeypatch):
    monkeypatch.setattr(main_mod, "_write_heartbeat", lambda: None)
    ha = FakeClient({"ok": False})
    mqtt = FakeClient(dict(BASE, status="connected", power=2000))
    app = build_app({}, ha_client=ha, mqtt_client=mqtt)
    app.tick()
    assert app.service.svc["/Status"] == 1  # connected
    assert app.service.svc["/Ac/Power"] == 2000.0


def test_tick_dry_run_no_crash(monkeypatch):
    monkeypatch.setattr(main_mod, "_write_heartbeat", lambda: None)
    service = EvChargerService(
        40,
        "0.1.0",
        custom_name="EV Charger",
        product_name="dbus-evcharger",
        connection="Local",
        on_mode=lambda m: None,
        on_startstop=lambda s: None,
        on_setcurrent=lambda c: None,
    )
    ha = HaClient(
        base_url="http://ha:8123",
        token="tok",
        status_entity="sensor.evcharger_status",
        power_entity="sensor.evcharger_power",
        current_entity="sensor.evcharger_current",
        energy_entity="sensor.evcharger_energy_total",
        timeout=3.0,
    )
    ha._configured = False
    app = main_mod.App(ha, None, service)
    result = app.tick()
    assert result is True


def test_empty_snapshot_keeps_last_known(monkeypatch):
    monkeypatch.setattr(main_mod, "_write_heartbeat", lambda: None)
    app = build_app(BASE)
    app.tick()
    app.tick()
    assert app.service.svc["/Status"] == 2  # charging persisted


def test_shutdown_does_not_crash(monkeypatch):
    monkeypatch.setattr(main_mod, "_write_heartbeat", lambda: None)
    service = EvChargerService(
        40,
        "0.1.0",
        custom_name="EV Charger",
        product_name="dbus-evcharger",
        connection="Local",
        on_mode=lambda m: None,
        on_startstop=lambda s: None,
        on_setcurrent=lambda c: None,
    )
    app = main_mod.App(FakeClient(BASE), None, service)
    app.shutdown()