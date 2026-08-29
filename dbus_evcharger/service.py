"""D-Bus service registration (Venus OS).

Registers:
  com.victronenergy.evcharger.<N>  - EV charger

Off-GX (tests / dev laptop) a NullDbusService stand-in is used so the module
imports cleanly without velib_python/dbus.
"""

import logging

logger = logging.getLogger(__name__)

VEDBUS_AVAILABLE = False
try:
    import sys

    sys.path.insert(0, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
    import dbus  # noqa: F401
    from vedbus import VeDbusService

    VEDBUS_AVAILABLE = True
except ImportError:
    logger.info("vedbus/dbus unavailable - using NullDbusService (off-GX mode)")


# --- charger status values (from Venus wiki) ----------------------------------
STATUS_DISCONNECTED = 0
STATUS_CONNECTED = 1
STATUS_CHARGING = 2
STATUS_CHARGED = 3
STATUS_WAITING_FOR_SUN = 4
STATUS_WAITING_FOR_RFID = 5
STATUS_WAITING_FOR_START = 6
STATUS_LOW_SOC = 7
STATUS_GROUND_TEST_ERROR = 8
STATUS_WELDED_CONTACTS_ERROR = 9
STATUS_CP_INPUT_TEST_ERROR = 10
STATUS_RESIDUAL_CURRENT_DETECTED = 11
STATUS_UNDERVOLTAGE_DETECTED = 12
STATUS_OVERVOLTAGE_DETECTED = 13
STATUS_OVERTEMPERATURE_DETECTED = 14
STATUS_RESERVED_15 = 15
STATUS_RESERVED_16 = 16
STATUS_RESERVED_17 = 17
STATUS_RESERVED_18 = 18
STATUS_RESERVED_19 = 19
STATUS_RESERVED_20 = 20
STATUS_STARTING_TO_CHARGE = 21
STATUS_SWITCHING_TO_3PHASE = 22
STATUS_SWITCHING_TO_1PHASE = 23
STATUS_STOPPING_CHARGING = 24

# --- charging mode values ------------------------------------------------------
MODE_MANUAL = 0
MODE_AUTO = 1
MODE_SCHEDULED = 2

# --- position values -----------------------------------------------------------
POSITION_AC_OUTPUT = 0  # charger draws from AC output (grid/inverter)
POSITION_AC_INPUT = 1  # charger feeds back (V2G / vehicle-to-home)


class NullDbusService:
    """Dict-like stand-in for VeDbusService used off-device."""

    def __init__(self, service_name: str, **_kwargs) -> None:
        self.service_name = service_name
        self.items: dict[str, object] = {}
        self._onchange: dict[str, callable] = {}

    def add_path(self, path, value, description="", writeable=False, onchangecallback=None, **_kw):
        self.items[path] = value
        if onchangecallback:
            self._onchange[path] = onchangecallback

    def __setitem__(self, path, value):
        old = self.items.get(path)
        self.items[path] = value
        cb = self._onchange.get(path)
        if cb and old != value:
            cb(path, value)  # match vedbus (path, value) signature

    def __getitem__(self, path):
        return self.items[path]

    def __delitem__(self, path):
        del self.items[path]


def _make_service(service_name: str):
    if VEDBUS_AVAILABLE:
        import dbus

        return VeDbusService(service_name, bus=dbus.SystemBus(private=True))
    return NullDbusService(service_name)


def _identity_paths(
    svc,
    product_name: str,
    version: str,
    custom_name: str,
    instance: int,
    connection: str,
):
    """Populate standard management + identity paths."""
    svc.add_path("/Mgmt/ProcessName", "dbus-evcharger")
    svc.add_path("/Mgmt/ProcessVersion", version)
    svc.add_path("/Mgmt/Connection", connection)
    svc.add_path("/DeviceInstance", instance)
    svc.add_path("/ProductId", 0xFFFF)  # generic/unknown product
    svc.add_path("/ProductName", product_name)
    svc.add_path("/FirmwareVersion", version)
    svc.add_path("/HardwareVersion", "n/a")
    svc.add_path("/Serial", f"dbusevcharger-{instance}")
    svc.add_path("/CustomName", custom_name)
    svc.add_path("/Connected", 1)


class EvChargerService:
    """Owns the single EV charger D-Bus service with all standard paths."""

    def __init__(
        self,
        instance: int,
        version: str,
        custom_name: str = "EV Charger",
        product_name: str = "EV Charger",
        connection: str = "Local",
        bus_suffix: str = "ha",
        on_mode=None,
        on_startstop=None,
        on_setcurrent=None,
    ) -> None:
        self.instance = instance
        # D-Bus bus names forbid digits after the last dot; instance lives in
        # /DeviceInstance only. Matches Victron convention (ttyO1, ha, etc.).
        bus_name = f"com.victronenergy.evcharger.{bus_suffix}"
        self.svc = _make_service(bus_name)
        _identity_paths(
            self.svc,
            product_name,
            version,
            custom_name,
            instance,
            connection,
        )

        # --- device info paths ---------------------------------------------
        self.svc.add_path("/Model", "")
        self.svc.add_path("/NrOfPhases", 1)

        # --- position: 0=AC Output, 1=AC Input (V2G) --------------------
        self.svc.add_path("/Position", POSITION_AC_OUTPUT)
        self.svc.add_path("/PositionIsAdjustable", 1)
        self.svc.add_path("/IsGenericEnergyMeter", 0)

        # --- AC measurement paths -----------------------------------------
        self.svc.add_path("/Ac/Power", 0)  # W
        self.svc.add_path("/Ac/Energy/Forward", 0)  # kWh total
        self.svc.add_path("/Ac/Frequency", 0)  # Hz
        self.svc.add_path("/Ac/L1/Power", 0)  # W
        self.svc.add_path("/Ac/L1/Voltage", 0)  # V
        self.svc.add_path("/Ac/L1/Current", 0)  # A
        self.svc.add_path("/Ac/L1/PowerFactor", 0)  # dimensionless
        self.svc.add_path("/Ac/L2/Power", 0)
        self.svc.add_path("/Ac/L2/Voltage", 0)
        self.svc.add_path("/Ac/L2/Current", 0)
        self.svc.add_path("/Ac/L2/PowerFactor", 0)
        self.svc.add_path("/Ac/L3/Power", 0)
        self.svc.add_path("/Ac/L3/Voltage", 0)
        self.svc.add_path("/Ac/L3/Current", 0)
        self.svc.add_path("/Ac/L3/PowerFactor", 0)

        # --- charging control paths (writable) ----------------------------
        self.svc.add_path("/Mode", MODE_AUTO, writeable=True, onchangecallback=on_mode)
        self.svc.add_path("/StartStop", 0, writeable=True, onchangecallback=on_startstop)
        self.svc.add_path("/AutoStart", 0, writeable=True)
        self.svc.add_path("/EnableDisplay", 1, writeable=True)

        # --- current control paths ----------------------------------------
        self.svc.add_path("/Current", 0)  # A actual
        self.svc.add_path(
            "/SetCurrent", 0, writeable=True, onchangecallback=on_setcurrent
        )  # A setpoint
        self.svc.add_path("/MinCurrent", 6, writeable=True)  # A minimum
        self.svc.add_path("/MaxCurrent", 32, writeable=True)  # A maximum

        # --- status path --------------------------------------------------
        self.svc.add_path("/Status", STATUS_DISCONNECTED)

        # --- session data -------------------------------------------------
        self.svc.add_path("/Session/Time", 0)  # seconds
        self.svc.add_path("/Session/Energy", 0)  # kWh
        self.svc.add_path("/Session/Cost", 0)
        self.svc.add_path("/Session/SavedCost", 0)
        self.svc.add_path("/Session/UserId", 0)
        self.svc.add_path("/Session/UserIdType", 0)

        # --- alarm paths (0=OK, 2=Alarm) ---------------------------------
        self.svc.add_path("/Alarms/GNDNotPresent", 0)
        self.svc.add_path("/Alarms/WeldedContacts", 0)
        self.svc.add_path("/Alarms/CPInputShortCircuit", 0)
        self.svc.add_path("/Alarms/ResidualCurrent", 0)
        self.svc.add_path("/Alarms/OverTemperature", 0)
        self.svc.add_path("/Alarms/LightSensorICFault", 0)
        self.svc.add_path("/Alarms/TamperDetected", 0)

        # --- warning paths (0=OK, 1=Warning) ------------------------------
        self.svc.add_path("/Alarms/SetupNeeded", 0)
        self.svc.add_path("/Alarms/BlockedWarning", 0)
        self.svc.add_path("/Alarms/HighTempWarning", 0)
        self.svc.add_path("/Alarms/GxCommWarning", 0)
        self.svc.add_path("/Alarms/OverloadDetected", 0)
        self.svc.add_path("/Alarms/OverloadActive", 0)
        self.svc.add_path("/Alarms/TimeSyncIssue", 0)
        self.svc.add_path("/Alarms/ExternalCurrentLimit", 0)
        self.svc.add_path("/Alarms/SystemCausedCurrentLimit", 0)
        self.svc.add_path("/Alarms/DisplayFWUpdateFailure1", 0)
        self.svc.add_path("/Alarms/DisplayFWUpdateFailure2", 0)
        self.svc.add_path("/Alarms/DisplayFWUpdateInProgress", 0)

    # --- updates -----------------------------------------------------------

    def update_charging(
        self,
        status: int,
        current: float,
        power: float,
        energy_forward: float,
        l1_power: float = 0,
        l1_voltage: float = 0,
        l1_current: float = 0,
        l1_power_factor: float = 0,
        frequency: float = 0,
        nr_of_phases: int = 1,
    ) -> None:
        """Update all charging metrics. All numeric values, not strings."""
        self.svc["/Status"] = status
        self.svc["/Current"] = round(current, 2)
        self.svc["/Ac/Power"] = round(power, 1)
        self.svc["/Ac/Energy/Forward"] = round(energy_forward, 3)
        self.svc["/Ac/Frequency"] = round(frequency, 2)
        self.svc["/NrOfPhases"] = nr_of_phases
        self.svc["/Ac/L1/Power"] = round(l1_power, 1)
        self.svc["/Ac/L1/Voltage"] = round(l1_voltage, 1)
        self.svc["/Ac/L1/Current"] = round(l1_current, 2)
        self.svc["/Ac/L1/PowerFactor"] = round(l1_power_factor, 3)

    def update_session(self, session_time: int, session_energy: float) -> None:
        """Update session counters."""
        self.svc["/Session/Time"] = session_time
        self.svc["/Session/Energy"] = round(session_energy, 3)

    def update_alarms(
        self,
        gnd_not_present: int = 0,
        welded_contacts: int = 0,
        cp_input_short: int = 0,
        residual_current: int = 0,
        over_temp: int = 0,
        **_,
    ) -> None:
        """Update alarm states. Values: 0=OK, 2=Alarm."""
        self.svc["/Alarms/GNDNotPresent"] = gnd_not_present
        self.svc["/Alarms/WeldedContacts"] = welded_contacts
        self.svc["/Alarms/CPInputShortCircuit"] = cp_input_short
        self.svc["/Alarms/ResidualCurrent"] = residual_current
        self.svc["/Alarms/OverTemperature"] = over_temp

    def set_connected(self, connected: bool) -> None:
        self.svc["/Connected"] = 1 if connected else 0

    def set_device_info(self, model: str = "", serial: str = "") -> None:
        if model:
            self.svc["/Model"] = model
        if serial:
            self.svc["/Serial"] = serial

    def set_mode_quietly(self, mode: int) -> None:
        """Set /Mode without re-triggering the onchange handler."""
        if isinstance(self.svc, NullDbusService):
            self.svc.items["/Mode"] = mode
        else:
            self.svc["/Mode"] = mode

    def set_current_quietly(self, current: float) -> None:
        """Set /SetCurrent without re-triggering the onchange handler."""
        if isinstance(self.svc, NullDbusService):
            self.svc.items["/SetCurrent"] = current
        else:
            self.svc["/SetCurrent"] = current
