"""Tests for dbus-evcharger L2 wiring."""

from dbus_evcharger.service import EvChargerService, NullDbusService


def build(**kw):
    kw.setdefault("on_mode", lambda m: None)
    kw.setdefault("on_startstop", lambda s: None)
    kw.setdefault("on_setcurrent", lambda c: None)
    return EvChargerService(
        instance=40,
        version="1.0.0",
        on_mode=kw["on_mode"],
        on_startstop=kw["on_startstop"],
        on_setcurrent=kw["on_setcurrent"],
    )


def test_update_charging_with_l2():
    s = build()
    assert isinstance(s.svc, NullDbusService)
    s.update_charging(
        status=2,
        current=40.0,
        power=9600.0,
        energy_forward=10.5,
        l1_power=4800.0,
        l1_voltage=240.0,
        l1_current=20.0,
        l1_power_factor=1.0,
        l2_power=4800.0,
        l2_voltage=240.0,
        l2_current=20.0,
        l2_power_factor=1.0,
        nr_of_phases=2,
    )
    assert s.svc["/Ac/L2/Current"] == 20.0
    assert s.svc["/Ac/L2/Power"] == 4800.0
    assert s.svc["/Ac/L2/Voltage"] == 240.0
    assert s.svc["/NrOfPhases"] == 2
    assert s.svc["/Current"] == 40.0
