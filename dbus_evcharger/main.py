"""Entry point: HA/MQTT -> D-Bus EV charger bridge."""

import argparse
import logging
import os
import signal
import sys
import time

from dbus_evcharger import config
from dbus_evcharger.ha_client import HaClient
from dbus_evcharger.mqtt_client import MqttClient
from dbus_evcharger.service import (
    STATUS_CHARGED,
    STATUS_CHARGING,
    STATUS_CONNECTED,
    STATUS_CP_INPUT_TEST_ERROR,
    STATUS_DISCONNECTED,
    STATUS_GROUND_TEST_ERROR,
    STATUS_LOW_SOC,
    STATUS_OVERTEMPERATURE_DETECTED,
    STATUS_OVERVOLTAGE_DETECTED,
    STATUS_RESIDUAL_CURRENT_DETECTED,
    STATUS_UNDERVOLTAGE_DETECTED,
    STATUS_WAITING_FOR_RFID,
    STATUS_WAITING_FOR_START,
    STATUS_WAITING_FOR_SUN,
    STATUS_WELDED_CONTACTS_ERROR,
    VEDBUS_AVAILABLE,
    EvChargerService,
)
from dbus_evcharger.voltage import GridVoltageReader

logger = logging.getLogger("dbus-evcharger")


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def build_service() -> EvChargerService:
    service = EvChargerService(
        instance=config.DEVICE_INSTANCE,
        version=config.SOFTWARE_VERSION,
        custom_name=config.DEFAULT_CUSTOM_NAME,
        product_name=config.PRODUCT_NAME,
        connection="Home Assistant" if config.ha_configured() else "Local",
        bus_suffix=config.BUS_SUFFIX,
        on_mode=lambda m: None,  # override in app if needed
        on_startstop=lambda ss: None,
        on_setcurrent=lambda sc: None,
    )
    # apply defaults
    service.svc["/Position"] = config.DEFAULT_POSITION
    service.svc["/MaxCurrent"] = config.DEFAULT_MAX_CURRENT
    service.svc["/MinCurrent"] = config.DEFAULT_MIN_CURRENT
    service.svc["/NrOfPhases"] = config.DEFAULT_NR_OF_PHASES
    service.set_device_info(model="Unknown", serial=f"dbusevcharger-{config.DEVICE_INSTANCE}")
    return service


class App:
    def __init__(
        self,
        ha_client: HaClient | None,
        mqtt_client: MqttClient | None,
        service: EvChargerService,
        voltage_reader: GridVoltageReader | None = None,
    ) -> None:
        self.ha_client = ha_client
        self.mqtt_client = mqtt_client
        self.service = service
        self.voltage_reader = voltage_reader or GridVoltageReader()
        self.last_ok_time: float | None = None
        # last successful poll
        self.loop_interval_ms = max(250, int(config.POLL_INTERVAL * 1000))
        self._last_commanded_mode: int | None = None  # track to avoid spamming
        self._last_commanded_startstop: int | None = None
        self._last_commanded_setcurrent: float | None = None

    # --- main loop --------------------------------------------------------
    def tick(self) -> bool:
        # try HA first, then MQTT, then fallback
        snapshot = {}
        source = None

        if self.ha_client and self.ha_client._configured:
            ha_data = self.ha_client.poll()
            if ha_data.get("ok"):
                snapshot.update(ha_data)
                source = "ha"
        if not source and self.mqtt_client and self.mqtt_client._configured:
            mqtt_data = self.mqtt_client.poll()
            if mqtt_data.get("ok"):
                snapshot.update(mqtt_data)
                source = "mqtt"

        now_ok = source is not None
        if now_ok:
            self.last_ok_time = _now()
        ha_reachable = (
            self.last_ok_time is not None and (_now() - self.last_ok_time) < config.HA_TIMEOUT * 3
        )
        self.service.set_connected(now_ok and ha_reachable)

        # refresh grid voltage (autodetected, cached)
        v_l1, v_l2 = self.voltage_reader.read()
        snapshot["l1_voltage"] = v_l1 if v_l1 is not None else snapshot.get("l1_voltage", 0)
        snapshot["l2_voltage"] = v_l2 if v_l2 is not None else snapshot.get("l2_voltage", 0)

        # update charging metrics if we have fresh data
        if now_ok:
            self._update_charging_from_snapshot(snapshot)
            self._update_session_from_snapshot(snapshot)
            self._handle_control_paths(snapshot)
        else:
            # stale: mark as unknown
            self.service.svc["/Status"] = STATUS_DISCONNECTED
            self.service.svc["/Current"] = None
            self.service.svc["/Ac/Power"] = None
            self.service.svc["/Ac/Energy/Forward"] = None

        _write_heartbeat()
        return True

    def _update_charging_from_snapshot(self, snap: dict) -> None:
        """Map snapshot keys to service paths."""
        status_map = {
            "disconnected": STATUS_DISCONNECTED,
            "connected": STATUS_CONNECTED,
            "charging": STATUS_CHARGING,
            "charged": STATUS_CHARGED,
            "waiting_for_sun": STATUS_WAITING_FOR_SUN,
            "waiting_for_rfid": STATUS_WAITING_FOR_RFID,
            "waiting_for_start": STATUS_WAITING_FOR_START,
            "low_soc": STATUS_LOW_SOC,
            "ground_test_error": STATUS_GROUND_TEST_ERROR,
            "welded_contacts_error": STATUS_WELDED_CONTACTS_ERROR,
            "cp_input_test_error": STATUS_CP_INPUT_TEST_ERROR,
            "residual_current": STATUS_RESIDUAL_CURRENT_DETECTED,
            "undervoltage": STATUS_UNDERVOLTAGE_DETECTED,
            "overvoltage": STATUS_OVERVOLTAGE_DETECTED,
            "overheating": STATUS_OVERTEMPERATURE_DETECTED,
        }
        status_str = str(snap.get("status", "")).lower().replace(" ", "_")
        status = status_map.get(status_str, STATUS_DISCONNECTED)

        power = snap.get("power", 0) or 0
        v_l1 = snap.get("l1_voltage", 0) or 0
        v_l2 = snap.get("l2_voltage", 0) or 0
        # Derive per-phase current: I = P / V (split-phase wallbox, equal load)
        i_l1 = power / v_l1 if v_l1 > 50 else (snap.get("l1_current", 0) or 0)
        i_l2 = power / v_l2 if v_l2 > 50 else (snap.get("l2_current", 0) or 0)

        self.service.update_charging(
            status=status,
            current=snap.get("current", 0),
            power=power,
            energy_forward=snap.get("energy_forward", 0),
            l1_power=snap.get("l1_power", 0) or (power / 2 if power else 0),
            l1_voltage=v_l1,
            l1_current=i_l1,
            l1_power_factor=snap.get("l1_power_factor", 0),
            l2_power=snap.get("l2_power", 0) or (power / 2 if power else 0),
            l2_voltage=v_l2,
            l2_current=i_l2,
            l2_power_factor=snap.get("l2_power_factor", 0),
            frequency=snap.get("frequency", 0),
            nr_of_phases=snap.get("nr_of_phases", 2),
        )

        # alarms
        self.service.update_alarms(
            gnd_not_present=snap.get("gnd_not_present", 0),
            welded_contacts=snap.get("welded_contacts", 0),
            cp_input_short=snap.get("cp_input_short", 0),
            residual_current=snap.get("residual_current", 0),
            over_temp=snap.get("over_temp", 0),
        )

    def _update_session_from_snapshot(self, snap: dict) -> None:
        self.service.update_session(
            session_time=snap.get("session_time", 0),
            session_energy=snap.get("session_energy", 0),
        )
        self.service.svc["/Session/Cost"] = snap.get("session_cost", 0)
        self.service.svc["/Session/SavedCost"] = snap.get("session_saved_cost", 0)
        self.service.svc["/Session/UserId"] = snap.get("session_user_id", 0)
        self.service.svc["/Session/UserIdType"] = snap.get("session_user_id_type", 0)

    def _handle_control_paths(self, snap: dict) -> None:
        """Apply manual overrides from HA/MQTT (if provided) to D-Bus."""
        # Mode
        mode_map = {"manual": 0, "auto": 1, "scheduled": 2}
        mode_str = str(snap.get("mode", "")).lower()
        mode = mode_map.get(mode_str, None)
        if mode is not None and mode != self._last_commanded_mode:
            logger.info("Setting mode to %s", mode)
            self.service.set_mode_quietly(mode)
            self._last_commanded_mode = mode

        # StartStop
        startstop = snap.get("startstop")
        if isinstance(startstop, (int, float)) and startstop != self._last_commanded_startstop:
            logger.info("Setting start/stop to %s", startstop)
            self.service.svc["/StartStop"] = int(startstop)
            self._last_commanded_startstop = int(startstop)

        # SetCurrent
        setcurrent = snap.get("setcurrent")
        if (
            isinstance(setcurrent, (int, float))
            and abs(setcurrent - (self._last_commanded_setcurrent or 0)) > 0.1
        ):
            logger.info("Setting set current to %.1f A", setcurrent)
            self.service.set_current_quietly(float(setcurrent))
            self._last_commanded_setcurrent = float(setcurrent)

    # --- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        logger.info("Shutting down")

    def serve(self) -> None:
        from gi.repository import GLib  # provided by Venus OS python env

        GLib.timeout_add(self.loop_interval_ms, self.tick)
        mainloop = GLib.MainLoop()

        def _stop(*_args):
            logger.info("Shutting down")
            self.shutdown()
            mainloop.quit()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        logger.info(
            "dbus-evcharger %s started (HA=%s, MQTT=%s)",
            config.SOFTWARE_VERSION,
            bool(self.ha_client and self.ha_client._configured),
            bool(self.mqtt_client and self.mqtt_client._configured),
        )
        mainloop.run()


def main() -> int:
    parser = argparse.ArgumentParser(description="HA/MQTT -> Venus OS D-Bus EV charger bridge")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run one control cycle against a NullDbusService and exit",
    )
    args = parser.parse_args()
    _setup_logging(args.debug)

    if args.dry_run:
        service = build_service()
        ha_client = (
            HaClient(
                base_url=config.HA_URL,
                token=config.HA_TOKEN,
                status_entity=config.HA_STATUS_ENTITY,
                power_entity=config.HA_POWER_ENTITY,
                current_entity=config.HA_CURRENT_ENTITY,
                energy_entity=config.HA_ENERGY_ENTITY,
                session_time_entity=config.HA_SESSION_TIME_ENTITY,
                startstop_entity=config.HA_STARTSTOP_ENTITY,
                setcurrent_entity=config.HA_SETCURRENT_ENTITY,
                timeout=config.HA_TIMEOUT,
            )
            if config.ha_configured()
            else None
        )
        mqtt_client = (
            MqttClient(
                host=config.MQTT_HOST,
                port=config.MQTT_PORT,
                username=config.MQTT_USERNAME,
                password=config.MQTT_PASSWORD,
                topic=config.MQTT_TOPIC,
                qos=config.MQTT_QOS,
            )
            if config.mqtt_configured()
            else None
        )
        app = App(ha_client, mqtt_client, service)
        app.tick()
        print("Service state:")
        for path, val in sorted(service.svc.items.items()):
            print(f"  {path}: {val}")
        return 0

    if not VEDBUS_AVAILABLE:
        logger.error("vedbus/dbus not available - run on the Cerbo GX")
        return 1
    from dbus.mainloop.glib import DBusGMainLoop

    DBusGMainLoop(set_as_default=True)
    ha_client = (
        HaClient(
            base_url=config.HA_URL,
            token=config.HA_TOKEN,
            status_entity=config.HA_STATUS_ENTITY,
            power_entity=config.HA_POWER_ENTITY,
            current_entity=config.HA_CURRENT_ENTITY,
            energy_entity=config.HA_ENERGY_ENTITY,
            session_time_entity=config.HA_SESSION_TIME_ENTITY,
            startstop_entity=config.HA_STARTSTOP_ENTITY,
            setcurrent_entity=config.HA_SETCURRENT_ENTITY,
            timeout=config.HA_TIMEOUT,
        )
        if config.ha_configured()
        else None
    )
    mqtt_client = (
        MqttClient(
            host=config.MQTT_HOST,
            port=config.MQTT_PORT,
            username=config.MQTT_USERNAME,
            password=config.MQTT_PASSWORD,
            topic=config.MQTT_TOPIC,
            qos=config.MQTT_QOS,
        )
        if config.mqtt_configured()
        else None
    )
    service = build_service()
    app = App(ha_client, mqtt_client, service)
    app.serve()
    return 0


def _now() -> float:
    return time.monotonic()


def _write_heartbeat() -> None:
    try:
        os.makedirs(os.path.dirname(config.HEARTBEAT_FILE), exist_ok=True)
        with open(config.HEARTBEAT_FILE, "w") as f:
            f.write(str(int(time.time())))
    except OSError as exc:  # /run may be read-only off-device
        logger.debug("heartbeat write failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
