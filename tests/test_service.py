"""Tests for dbus-evcharger service."""

from dbus_evcharger.service import EvChargerService, NullDbusService


def build(**kw):
    kw.setdefault("on_mode", lambda m: None)
    kw.setdefault("on_startstop", lambda s: None)
    kw.setdefault("on_setcurrent", lambda c: None)
    # bus_suffix="charger" mirrors actual runtime (config.BUS_SUFFIX default)
    kw.setdefault("bus_suffix", "charger")
    return EvChargerService(
        instance=40,
        version="0.1.0",
        product_name="dbus-evcharger",
        custom_name="EV Charger",
        connection="Local",
        **kw,
    )


def test_service_name():
    s = build()
    # D-Bus bus names forbid digits after the last dot; instance lives in
    # /DeviceInstance, suffix is textual. Default suffix "charger" must
    # differ from dbus-ev's "ha" to avoid well-known-name collision.
    assert s.svc.service_name == "com.victronenergy.evcharger.charger"


def test_service_name_custom_suffix():
    s = EvChargerService(
        instance=22,
        version="0.1.0",
        bus_suffix="ttyO1",
    )
    assert s.svc.service_name == "com.victronenergy.evcharger.ttyO1"


def test_identity_paths_present():
    s = build()
    for p in (
        "/Mgmt/ProcessName",
        "/Mgmt/ProcessVersion",
        "/Mgmt/Connection",
        "/DeviceInstance",
        "/ProductId",
        "/ProductName",
        "/CustomName",
        "/Connected",
        "/Serial",
    ):
        assert p in s.svc.items, f"missing {p}"


def test_device_info_paths():
    s = build()
    s.set_device_info(model="Test Charger", serial="EV123456")
    assert s.svc["/Model"] == "Test Charger"
    assert s.svc["/Serial"] == "EV123456"


def test_charging_update():
    s = build()
    s.update_charging(
        status=2,
        current=16.5,
        power=3800.0,
        energy_forward=12.5,
        l1_power=3800.0,
        l1_voltage=230.0,
        l1_current=16.5,
        l1_power_factor=0.98,
        frequency=50.0,
        nr_of_phases=1,
    )
    assert s.svc["/Status"] == 2
    assert s.svc["/Current"] == 16.5
    assert s.svc["/Ac/Power"] == 3800.0
    assert s.svc["/Ac/Energy/Forward"] == 12.5
    assert s.svc["/Ac/L1/Power"] == 3800.0
    assert s.svc["/Ac/L1/Voltage"] == 230.0
    assert s.svc["/Ac/L1/Current"] == 16.5
    assert s.svc["/Ac/L1/PowerFactor"] == 0.98
    assert s.svc["/Ac/Frequency"] == 50.0
    assert s.svc["/NrOfPhases"] == 1


def test_session_update():
    s = build()
    s.update_session(session_time=3600, session_energy=5.5)
    assert s.svc["/Session/Time"] == 3600
    assert s.svc["/Session/Energy"] == 5.5


def test_alarms_update():
    s = build()
    s.update_alarms(
        gnd_not_present=2,
        welded_contacts=0,
        cp_input_short=0,
        residual_current=2,
        over_temp=0,
    )
    assert s.svc["/Alarms/GNDNotPresent"] == 2
    assert s.svc["/Alarms/WeldedContacts"] == 0
    assert s.svc["/Alarms/CPInputShortCircuit"] == 0
    assert s.svc["/Alarms/ResidualCurrent"] == 2
    assert s.svc["/Alarms/OverTemperature"] == 0


def test_set_mode_quietly():
    seen = []
    s = build(on_mode=lambda m: seen.append(m))
    s.set_mode_quietly(2)
    assert seen == []
    assert s.svc["/Mode"] == 2


def test_set_current_quietly():
    seen = []
    s = build(on_setcurrent=lambda c: seen.append(c))
    s.set_current_quietly(16.0)
    assert seen == []
    assert s.svc["/SetCurrent"] == 16.0


def test_connected_propagates():
    s = build()
    s.set_connected(False)
    assert s.svc["/Connected"] == 0
    s.set_connected(True)
    assert s.svc["/Connected"] == 1


def test_null_service_onchange_fires():
    seen = []
    svc = NullDbusService("test")
    svc.add_path("/Mode", 0, writeable=True, onchangecallback=lambda p, v: seen.append(v))
    svc["/Mode"] = 2
    assert seen == [2]
    svc["/Mode"] = 2  # no change -> no callback
    assert seen == [2]


def test_null_service_setitem_triggers_callback():
    seen = []
    svc = NullDbusService("test")
    svc.add_path("/SetCurrent", 0, writeable=True, onchangecallback=lambda p, v: seen.append(v))
    svc["/SetCurrent"] = 16.0
    assert seen == [16.0]
    svc["/SetCurrent"] = 16.0  # no change
    assert seen == [16.0]
    svc["/SetCurrent"] = 32.0  # change
    assert seen == [16.0, 32.0]
