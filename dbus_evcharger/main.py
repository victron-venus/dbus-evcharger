"""Entry point: HA/MQTT -> D-Bus EV charger bridge."""

import argparse
import logging
import math
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
from dbus_evcharger.worker import PollWorker

logger = logging.getLogger("dbus-evcharger")

_STATUS_MAP = {
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


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _usable(snapshot):
    return (
        snapshot.get("ok")
        and snapshot.get("status") is not None
        and str(snapshot["status"]).strip().lower().replace(" ", "_") in _STATUS_MAP
        and _finite(snapshot.get("power")) is not None
    )


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
        register=False,
    )
    # apply defaults
    service.svc["/Position"] = config.DEFAULT_POSITION
    service.svc["/MaxCurrent"] = config.DEFAULT_MAX_CURRENT
    service.svc["/MinCurrent"] = config.DEFAULT_MIN_CURRENT
    service.svc["/NrOfPhases"] = config.DEFAULT_NR_OF_PHASES
    service.set_device_info(model="Unknown", serial=f"dbusevcharger-{config.DEVICE_INSTANCE}")
    service.set_connected(False)
    service.register()
    return service


class App:
    """Publish fresh source telemetry without blocking the D-Bus main loop."""

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
        self.loop_interval_ms = max(250, min(1000, int(config.POLL_INTERVAL * 1000)))
        self._poll_interval = max(0.25, config.POLL_INTERVAL)
        self._next_poll_at = None
        self._last_commanded_mode: int | None = None  # track to avoid spamming
        self._last_commanded_startstop: int | None = None
        self._last_commanded_setcurrent: float | None = None
        self._worker = None
        self._snapshot = {}
        self._snapshot_at = None

    # --- main loop --------------------------------------------------------
    def _collect_snapshot(self):
        """Collect blocking inputs on the worker, without touching D-Bus paths."""
        # MQTT snapshots are nonblocking and belong to main-loop publication.
        snapshot = {}
        source = None

        if self.ha_client and self.ha_client._configured:
            ha_data = self.ha_client.poll()
            if _usable(ha_data):
                snapshot.update(ha_data)
                source = "ha"
        # refresh grid voltage (autodetected, cached)
        v_l1, v_l2 = self.voltage_reader.read()
        snapshot["l1_voltage"] = v_l1 if v_l1 is not None else snapshot.get("l1_voltage")
        snapshot["l2_voltage"] = v_l2 if v_l2 is not None else snapshot.get("l2_voltage")
        snapshot["ok"] = source is not None
        return snapshot

    def _accept_snapshot(self, snapshot, started):
        self._snapshot = snapshot
        self._snapshot_at = started
        self._publish_snapshot()

    def tick(self) -> bool:
        if self._worker is None:
            # Explicit one-shot/dry-run use; serve() always installs a worker.
            started = _now()
            self._accept_snapshot(self._collect_snapshot(), started)
            return True
        now = _now()
        if (self._next_poll_at is None or now >= self._next_poll_at) and self._worker.poll(
            self._accept_snapshot
        ):
            self._next_poll_at = now + self._poll_interval
        return self._publish_snapshot()

    def _publish_snapshot(self) -> bool:
        """Publish on GLib delivery/ticks; only HA/grid collection is scheduled."""
        now = _now()
        snapshot = dict(self._snapshot)
        for field, deadline in snapshot.pop("_expires_at", {}).items():
            if now >= deadline:
                snapshot[field] = None
        ttl = max(config.HA_TIMEOUT * 3, config.POLL_INTERVAL * 2)
        collected_fresh = self._snapshot_at is not None and 0 <= now - self._snapshot_at < ttl
        using_ha = collected_fresh and _usable(snapshot)
        if not using_ha:
            # One paho thread maintains this cache; poll never waits for DNS/TCP.
            mqtt_data = (
                self.mqtt_client.poll()
                if self.mqtt_client is not None and self.mqtt_client._configured
                else {}
            )
            snapshot = dict(mqtt_data)
            for field, deadline in snapshot.pop("_expires_at", {}).items():
                if now >= deadline:
                    snapshot[field] = None
            for field in ("l1_voltage", "l2_voltage"):
                voltage = self._snapshot.get(field) if collected_fresh else None
                if voltage is not None:
                    snapshot[field] = voltage
        now_ok = _usable(snapshot)
        self.service.set_connected(bool(now_ok))
        if now_ok:
            self.last_ok_time = self._snapshot_at if using_ha else now

        # update charging metrics if we have fresh data
        if now_ok:
            self._update_charging_from_snapshot(snapshot)
            self._update_session_from_snapshot(snapshot)
            self._handle_control_paths(snapshot)
        else:
            # stale: mark as unknown
            self.service.svc["/Status"] = STATUS_DISCONNECTED
            for path in (
                "/Current",
                "/Ac/Power",
                "/Ac/Energy/Forward",
                "/Ac/Frequency",
                "/Ac/L1/Power",
                "/Ac/L1/Voltage",
                "/Ac/L1/Current",
                "/Ac/L1/PowerFactor",
                "/Ac/L2/Power",
                "/Ac/L2/Voltage",
                "/Ac/L2/Current",
                "/Ac/L2/PowerFactor",
                "/Session/Time",
                "/Session/Energy",
            ):
                self.service.svc[path] = None

        _write_heartbeat()
        return True

    def _update_charging_from_snapshot(self, snap: dict) -> None:
        """Map snapshot keys to service paths."""
        status_str = str(snap.get("status", "")).strip().lower().replace(" ", "_")
        status = _STATUS_MAP.get(status_str, STATUS_DISCONNECTED)

        power = _finite(snap.get("power"))
        v_l1 = _finite(snap.get("l1_voltage"))
        v_l2 = _finite(snap.get("l2_voltage"))
        phases = _finite(snap.get("nr_of_phases", config.DEFAULT_NR_OF_PHASES))
        phases = int(phases) if phases in (1, 2) else config.DEFAULT_NR_OF_PHASES
        p_l1 = _finite(snap.get("l1_power"))
        p_l2 = _finite(snap.get("l2_power"))
        if p_l1 is None and power is not None:
            p_l1 = power / phases
        if p_l2 is None and power is not None and phases == 2:
            p_l2 = power / phases
        # Each phase's current uses that phase's power, not total charger power.
        i_l1 = _finite(snap.get("l1_current"))
        i_l2 = _finite(snap.get("l2_current"))
        if i_l1 is None and p_l1 is not None and v_l1 is not None and v_l1 > 50:
            i_l1 = p_l1 / v_l1
        if i_l2 is None and p_l2 is not None and v_l2 is not None and v_l2 > 50:
            i_l2 = p_l2 / v_l2

        self.service.update_charging(
            status=status,
            current=_finite(snap.get("current")),
            power=power,
            l1_power=p_l1,
            l1_voltage=v_l1,
            l1_current=i_l1,
            l1_power_factor=_finite(snap.get("l1_power_factor")),
            l2_power=p_l2,
            l2_voltage=v_l2,
            l2_current=i_l2,
            l2_power_factor=_finite(snap.get("l2_power_factor")),
            frequency=_finite(snap.get("frequency")),
            nr_of_phases=phases,
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
        # HA sensor.home_2_1d = Emporia daily consumption (resets midnight) — not lifetime.
        # Map to /Session/Energy (current session) instead of /Ac/Energy/Forward (lifetime).
        session_energy = snap.get("session_energy")
        if session_energy is None:
            session_energy = snap.get("energy_forward")
        self.service.update_session(
            session_time=_finite(snap.get("session_time")),
            session_energy=_finite(session_energy),
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
        if startstop in (0, 1) and startstop != self._last_commanded_startstop:
            logger.info("Setting start/stop to %s", startstop)
            self.service.svc["/StartStop"] = int(startstop)
            self._last_commanded_startstop = int(startstop)

        # SetCurrent
        setcurrent = _finite(snap.get("setcurrent"))
        if (
            setcurrent is not None
            and 0 <= setcurrent <= config.DEFAULT_MAX_CURRENT
            and abs(setcurrent - (self._last_commanded_setcurrent or 0)) > 0.1
        ):
            logger.info("Setting set current to %.1f A", setcurrent)
            self.service.set_current_quietly(float(setcurrent))
            self._last_commanded_setcurrent = float(setcurrent)

    # --- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        logger.info("Shutting down")
        if self._worker is not None:
            self._worker.stop()
        else:
            self._close_clients()

    def _close_clients(self):
        if self.ha_client is not None and hasattr(self.ha_client, "close"):
            self.ha_client.close()
        if self.mqtt_client is not None:
            self.mqtt_client.disconnect()

    def serve(self) -> None:
        from gi.repository import GLib  # provided by Venus OS python env

        self._worker = PollWorker(self._collect_snapshot, GLib.idle_add, self._close_clients)
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
