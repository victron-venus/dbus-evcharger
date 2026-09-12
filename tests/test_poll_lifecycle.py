"""Regression coverage for blocked inputs, invalid values and source recovery."""

# White-box lifecycle tests exercise private callbacks without live D-Bus/network.
# pylint: disable=protected-access

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dbus_evcharger import main, voltage
from dbus_evcharger.ha_client import HaClient, build_template
from dbus_evcharger.worker import PollWorker
from tests.test_app_tick import BASE, FakeClient, build_app


def test_blocked_poll_keeps_tick_responsive_and_delivers_on_main_loop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    dispatched = []
    closed = []
    app = build_app(BASE)
    app.voltage_reader = SimpleNamespace(read=lambda: (120.0, 120.0))
    monkeypatch.setattr(main, "_write_heartbeat", lambda: None)

    def poll():
        entered.set()
        assert release.wait(2)
        return dict(BASE, ok=True, nr_of_phases=2)

    app.ha_client.poll = poll
    worker = PollWorker(
        app._collect_snapshot, lambda *args: dispatched.append(args), lambda: closed.append(True)
    )
    app._worker = worker
    try:
        start = time.monotonic()
        assert app.tick() is True
        assert entered.wait(1)
        for _ in range(10):
            assert app.tick() is True
        assert time.monotonic() - start < 0.5
        assert app.service.svc["/Connected"] == 0
        assert not dispatched
        release.set()
        deadline = time.monotonic() + 1
        while not dispatched and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(dispatched) == 1
        # The worker must not mutate any D-Bus value before main-loop delivery.
        assert app.service.svc["/Connected"] == 0
        callback, *args = dispatched.pop()
        assert callback(*args) is False
        app.tick()
        assert app.service.svc["/Ac/Power"] == 3800.0
        assert app.service.svc["/Ac/L1/Current"] == round(1900 / 120, 2)
    finally:
        release.set()
        worker.stop()
    assert closed == [True]


def test_worker_stops_without_delivering_late_results():
    entered, release = threading.Event(), threading.Event()
    delivered, dispatched, closed = [], [], []

    def collect():
        entered.set()
        assert release.wait(2)
        return {"ok": True}

    worker = PollWorker(collect, lambda *args: dispatched.append(args), lambda: closed.append(True))
    try:
        assert worker.poll(lambda *args: delivered.append(args))
        assert entered.wait(1)
        assert not worker.poll(lambda *_: None)
        assert worker.stop(timeout=0.01) is False
    finally:
        release.set()
        worker.stop()
    for callback, *args in dispatched:
        callback(*args)
    assert not delivered
    assert closed == [True]
    assert not worker.poll(lambda *_: None)


def test_worker_exception_delivers_failed_snapshot_and_recovers():
    completed = threading.Event()
    dispatched = []
    collect = Mock(side_effect=[ValueError("invalid source"), dict(BASE, ok=True)])

    def dispatch(*args):
        dispatched.append(args)
        completed.set()

    worker = PollWorker(collect, dispatch, lambda: None)
    results = []
    try:
        for _ in range(2):
            completed.clear()
            assert worker.poll(lambda snapshot, _: results.append(snapshot))
            assert completed.wait(1)
            callback, *args = dispatched.pop(0)
            callback(*args)
        assert results[0] == {"ok": False}
        assert results[1]["power"] == BASE["power"]
    finally:
        worker.stop()


def test_stale_worker_snapshot_invalidates_all_measurements(monkeypatch):
    app = build_app(BASE)
    app._worker = SimpleNamespace(poll=lambda *_: False)
    monkeypatch.setattr(main, "_write_heartbeat", lambda: None)
    monkeypatch.setattr(main.config, "HA_TIMEOUT", 3.0)
    monkeypatch.setattr(main.config, "POLL_INTERVAL", 1.0)
    app._accept_snapshot(dict(BASE, ok=True, l1_voltage=120, l2_voltage=120), 100.0)
    monkeypatch.setattr(main, "_now", lambda: 100.0)
    app.tick()
    assert app.service.svc["/Connected"] == 1
    monkeypatch.setattr(main, "_now", lambda: 109.0)
    app.tick()
    assert app.service.svc["/Connected"] == 0
    for path in ["/Ac/Power", "/Current", "/Ac/L1/Voltage", "/Ac/L2/Current", "/Session/Energy"]:
        assert app.service.svc[path] is None


def test_queued_mqtt_fields_keep_their_original_expiry(monkeypatch):
    app = build_app(BASE)
    app._worker = SimpleNamespace(poll=lambda *_: False)
    monkeypatch.setattr(main, "_write_heartbeat", lambda: None)
    monkeypatch.setattr(main, "_now", lambda: 102.0)
    snapshot = dict(BASE, ok=True, _expires_at={"current": 101, "status": 103, "power": 103})
    app._accept_snapshot(snapshot, 100)
    app.tick()
    assert app.service.svc["/Connected"] == 1
    assert app.service.svc["/Current"] is None
    monkeypatch.setattr(main, "_now", lambda: 103.0)
    app.tick()
    assert app.service.svc["/Connected"] == 0
    assert app.service.svc["/Ac/Power"] is None


def test_zero_session_and_phase_power_are_not_replaced(monkeypatch):
    app = build_app(dict(BASE, session_energy=0, energy_forward=10, l1_power=0, l2_power=3800))
    app.voltage_reader = SimpleNamespace(read=lambda: (120, 120))
    monkeypatch.setattr(main, "_write_heartbeat", lambda: None)
    app.tick()
    assert app.service.svc["/Session/Energy"] == 0
    assert app.service.svc["/Ac/L1/Power"] == 0
    assert app.service.svc["/Ac/L1/Current"] == 0


@pytest.mark.parametrize("invalid", [None, "unknown", "unavailable", float("nan"), float("inf")])
def test_invalid_power_cannot_publish_connected_zero(monkeypatch, invalid):
    app = build_app(dict(BASE, power=invalid), mqtt_client=FakeClient({}, configured=False))
    app.voltage_reader = SimpleNamespace(read=lambda: (None, None))
    monkeypatch.setattr(main, "_write_heartbeat", lambda: None)
    assert app.tick()
    assert app.service.svc["/Connected"] == 0
    assert app.service.svc["/Ac/Power"] is None


def test_unknown_status_cannot_publish_connected_telemetry(monkeypatch):
    app = build_app(dict(BASE, status="garbage"), mqtt_client=FakeClient({}, configured=False))
    app.voltage_reader = SimpleNamespace(read=lambda: (None, None))
    monkeypatch.setattr(main, "_write_heartbeat", lambda: None)
    app.tick()
    assert app.service.svc["/Connected"] == 0
    assert app.service.svc["/Ac/Power"] is None


def test_status_whitespace_has_consistent_validity_and_mapping(monkeypatch):
    app = build_app(dict(BASE, status=" charging "))
    app.voltage_reader = SimpleNamespace(read=lambda: (120, 120))
    monkeypatch.setattr(main, "_write_heartbeat", lambda: None)
    app.tick()
    assert app.service.svc["/Connected"] == 1
    assert app.service.svc["/Status"] == main.STATUS_CHARGING


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("double 1.2e2", 120.0),
        ("double -1", None),
        ("double nan", None),
        ("double inf", None),
        ("double invalid", None),
        ("variant <int32 0>", None),
    ],
)
def test_voltage_parser_rejects_invalid_values(monkeypatch, raw, expected):
    monkeypatch.setattr(
        voltage.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=raw)
    )
    assert voltage.GridVoltageReader()._read_dbus("grid/Ac/L1/Voltage") == expected


@pytest.mark.parametrize("invalid", ["nan", "inf", "-inf", "unknown", "unavailable"])
def test_ha_nonfinite_optional_values_remain_invalid(monkeypatch, invalid):
    client = HaClient(
        "http://ha", "test", "sensor.status", "sensor.power", "sensor.current", "sensor.energy"
    )
    monkeypatch.setattr(
        client._session,
        "post",
        lambda *a, **k: SimpleNamespace(
            status_code=200,
            text=json.dumps(
                {
                    "status": "charging",
                    "power": "0",
                    "current": invalid,
                    "session_time": invalid,
                    "startstop": invalid,
                    "setcurrent": invalid,
                }
            ),
        ),
    )
    result = client.poll()
    assert result["ok"]
    assert result["power"] == 0
    assert all(
        result[key] is None for key in ["current", "session_time", "startstop", "setcurrent"]
    )


def test_ha_template_preserves_unavailable_states():
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment()
    env.filters["to_json"] = json.dumps
    output = env.from_string(
        build_template("sensor.status", "sensor.power", "sensor.current", "sensor.energy")
    ).render(states=lambda _: "unavailable")
    assert json.loads(output)["power"] == "unavailable"


def test_ha_non_object_json_is_failed_snapshot(monkeypatch):
    client = HaClient("http://ha", "test", "status", "power", "current", "energy")
    monkeypatch.setattr(
        client._session, "post", lambda *a, **k: SimpleNamespace(status_code=200, text="[]")
    )
    assert client.poll()["ok"] is False


def test_voltage_loss_clears_values_and_rediscovers(monkeypatch):
    monkeypatch.setattr(voltage, "dbus", object())
    now = [100.0]
    monkeypatch.setattr(voltage.time, "monotonic", lambda: now[0])
    reader = voltage.GridVoltageReader()
    discover = Mock(side_effect=["grid.old", "grid.new"])
    monkeypatch.setattr(reader, "_discover_grid_service", discover)
    monkeypatch.setattr(reader, "_read_dbus", Mock(side_effect=[120, 121, None, None, 122, 123]))
    assert reader.read() == (120, 121)
    assert reader.read() == (None, None)
    assert reader.read() == (None, None)
    assert discover.call_count == 1
    now[0] = 106
    assert reader.read() == (122, 123)
    assert discover.call_count == 2
