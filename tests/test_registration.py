"""Bus names must become visible only after complete service initialization."""

import sys
from types import SimpleNamespace

import pytest

from dbus_evcharger import main, service


@pytest.fixture(name="native")
def native_service(monkeypatch):
    """Record registration-time values without a real system bus."""
    state = SimpleNamespace(instances=[], snapshots=[], failure=None)

    class NativeService(service.NullDbusService):
        """Model deferred well-known-name acquisition and immutable path setup."""

        def __init__(self, name, *, bus, register):
            assert register is False
            assert bus is state.bus
            super().__init__(name)
            self.registered = False
            state.instances.append(self)

        def add_path(
            self, path, value, description="", writeable=False, onchangecallback=None, **kwargs
        ):
            assert not self.registered
            if path == state.failure:
                raise ValueError("invalid initialization")
            super().add_path(path, value, description, writeable, onchangecallback, **kwargs)

        def register(self):
            assert not self.registered
            self.registered = True
            state.snapshots.append(dict(self.items))

    state.bus = object()

    def system_bus(*, private):
        assert private is True
        return state.bus

    dbus = SimpleNamespace(SystemBus=system_bus)
    monkeypatch.setitem(sys.modules, "dbus", dbus)
    monkeypatch.setattr(service, "VEDBUS_AVAILABLE", True)
    monkeypatch.setattr(service, "VeDbusService", NativeService, raising=False)
    return state


def test_native_constructor_registers_complete_paths_once(native):
    charger = service.EvChargerService(instance=40, version="test")
    assert len(native.snapshots) == 1
    snapshot = native.snapshots[0]
    for path in (
        "/Mgmt/ProcessName",
        "/Mgmt/ProcessVersion",
        "/Mgmt/Connection",
        "/DeviceInstance",
        "/ProductId",
        "/ProductName",
        "/FirmwareVersion",
        "/Connected",
        "/Serial",
        "/Ac/L3/Power",
        "/Mode",
        "/SetCurrent",
        "/Session/Energy",
        "/Alarms/DisplayFWUpdateInProgress",
    ):
        assert path in snapshot
    assert snapshot == charger.svc.items
    charger.register()
    assert len(native.snapshots) == 1


def test_builder_registers_configured_values_and_disconnected_state(native, monkeypatch):
    monkeypatch.setattr(main.config, "DEFAULT_POSITION", 1)
    monkeypatch.setattr(main.config, "DEFAULT_MAX_CURRENT", 24)
    monkeypatch.setattr(main.config, "DEFAULT_MIN_CURRENT", 8)
    monkeypatch.setattr(main.config, "DEFAULT_NR_OF_PHASES", 2)
    charger = main.build_service()
    snapshot = native.snapshots[0]
    assert snapshot["/Connected"] == 0
    assert snapshot["/Position"] == 1
    assert snapshot["/MaxCurrent"] == 24
    assert snapshot["/MinCurrent"] == 8
    assert snapshot["/NrOfPhases"] == 2
    assert snapshot["/Model"] == "Unknown"
    assert snapshot == charger.svc.items


def test_initialization_failure_does_not_publish_partial_service(native):
    native.failure = "/Alarms/DisplayFWUpdateInProgress"
    with pytest.raises(ValueError, match="invalid initialization"):
        main.build_service()
    assert not native.snapshots
