"""Tests for HA client."""

import json
from unittest.mock import MagicMock, patch

from dbus_evcharger.ha_client import (
    CircuitBreaker,
    HaClient,
    build_template,
)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_client(**kw):
    defaults = {
        "base_url": "http://ha:8123",
        "token": "tok",
        "status_entity": "sensor.evcharger_status",
        "power_entity": "sensor.evcharger_power",
        "current_entity": "sensor.evcharger_current",
        "energy_entity": "sensor.evcharger_energy_total",
        "session_time_entity": "sensor.evcharger_session_time",
        "startstop_entity": "switch.evcharger_startstop",
        "setcurrent_entity": "number.evcharger_setcurrent",
        "timeout": 3.0,
    }
    defaults.update(kw)
    return HaClient(**defaults)


def template_response(
    status="charging",
    power="3800",
    current="16",
    energy="12.5",
    session_time="3600",
    startstop="1",
    setcurrent="16",
):
    payload = {
        "status": status,
        "power": power,
        "current": current,
        "energy_forward": energy,
        "session_time": session_time,
        "startstop": startstop,
        "setcurrent": setcurrent,
    }
    resp = MagicMock(status_code=200, text=json.dumps(payload))
    return resp


def test_build_template_contains_entities():
    t = build_template(
        "sensor.evcharger_status",
        "sensor.evcharger_power",
        "sensor.evcharger_current",
        "sensor.evcharger_energy_total",
        "sensor.evcharger_session_time",
        "switch.evcharger_startstop",
        "number.evcharger_setcurrent",
    )
    assert "sensor.evcharger_status" in t
    assert "sensor.evcharger_power" in t
    assert "sensor.evcharger_current" in t
    assert "sensor.evcharger_energy_total" in t


def test_build_template_empty_optional_entities():
    """Empty optional entities produce 'none' literal in template."""
    t = build_template(
        status_entity="sensor.evcharger_status",
        power_entity="sensor.evcharger_power",
        current_entity="sensor.evcharger_current",
        energy_entity="sensor.evcharger_energy_total",
        session_time_entity="",
        startstop_entity="",
        setcurrent_entity="",
    )
    # Required entities are present
    assert "sensor.evcharger_status" in t
    assert "sensor.evcharger_power" in t
    assert "sensor.evcharger_current" in t
    assert "sensor.evcharger_energy_total" in t
    # Empty optional entities produce 'none' branch (template has whitespace)
    assert "'session_time': {% if '' != '' %}" in t
    assert "'startstop': {% if '' != '' %}" in t
    assert "'setcurrent': {% if '' != '' %}" in t
    # Each optional entity has a 'none' fallback branch
    assert t.count("none") >= 3


@patch("dbus_evcharger.ha_client.requests.Session.post")
def test_poll_success(post):
    post.return_value = template_response()
    c = make_client()
    r = c.poll()
    assert r["ok"] is True
    assert r["status"] == "charging"
    assert r["power"] == 3800.0
    assert r["current"] == 16.0
    assert r["energy_forward"] == 12.5
    assert r["session_time"] == 3600
    assert r["startstop"] == 1
    assert r["setcurrent"] == 16.0


@patch("dbus_evcharger.ha_client.requests.Session.post")
def test_poll_empty_optional_entities_return_none(post):
    """Empty optional entities should return None in poll result."""
    c = make_client(
        session_time_entity="",
        startstop_entity="",
        setcurrent_entity="",
    )
    # When optional entities are empty, template emits "none" -> poll returns None
    post.return_value = template_response(session_time="none", startstop="none", setcurrent="none")
    r = c.poll()
    assert r["ok"] is True
    assert r["status"] == "charging"
    assert r["session_time"] is None
    assert r["startstop"] is None
    assert r["setcurrent"] is None


@patch("dbus_evcharger.ha_client.requests.Session.post")
def test_poll_nonnumeric_values_return_none(post):
    post.return_value = template_response(power="invalid", current="unknown")
    c = make_client()
    r = c.poll()
    assert r["ok"] is True
    assert r["power"] is None
    assert r["current"] is None


@patch("dbus_evcharger.ha_client.requests.Session.post")
def test_poll_failure_serves_last_known(post):
    post.return_value = template_response()
    c = make_client()
    first = c.poll()
    assert first["ok"] is True

    from requests.exceptions import Timeout

    post.side_effect = Timeout("boom")
    second = c.poll()
    assert second["ok"] is False
    assert second["status"] == "charging"  # last-known served
    assert second["power"] == 3800.0


@patch("dbus_evcharger.ha_client.requests.Session.post")
def test_circuit_breaker_opens_and_resets(post):
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=3, reset_timeout=60.0)

    import dbus_evcharger.ha_client as mod

    real_monotonic = mod.time.monotonic
    mod.time.monotonic = lambda: clock.t

    try:
        from requests.exceptions import ConnectionError as ReqConnError

        post.side_effect = ReqConnError("down")
        c = make_client(breaker=breaker)
        for _ in range(3):
            c.poll()
        assert breaker.is_open is True
        calls_before = post.call_count
        c.poll()
        assert post.call_count == calls_before  # no call when open

        clock.advance(61)  # past reset timeout -> half-open allows retry
        assert breaker.is_open is False
        post.side_effect = None
        post.return_value = template_response()
        r = c.poll()
        assert r["ok"] is True
        assert breaker.is_open is False
    finally:
        mod.time.monotonic = real_monotonic


def test_unconfigured_client_logs_proper_message():
    c = HaClient(
        base_url="",
        token="",
        status_entity="sensor.evcharger_status",
        power_entity="sensor.evcharger_power",
        current_entity="sensor.evcharger_current",
        energy_entity="sensor.evcharger_energy_total",
    )
    r = c.poll()
    assert r["ok"] is False
    assert c._configured is False
