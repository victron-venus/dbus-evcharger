"""Grid meter voltage reader via D-Bus.

Autodetects com.victronenergy.grid.* services and reads L1/L2 voltage
for current derivation: I = P / V (240V split-phase wallbox).
"""

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

VEDBUS_AVAILABLE = False
try:
    import dbus
except ImportError:
    dbus = None  # type: ignore


class GridVoltageReader:
    """Read L1/L2 voltage from autodetected grid meter."""

    def __init__(self) -> None:
        self._voltage_l1: float | None = None
        self._voltage_l2: float | None = None
        self._grid_service: str | None = None

    def read(self) -> tuple[float | None, float | None]:
        """Poll voltages. Returns (l1_voltage, l2_voltage) or (None, None)."""
        # lazy discovery
        if self._grid_service is None and dbus is not None:
            self._grid_service = self._discover_grid_service()

        if self._grid_service is None:
            return None, None

        if dbus is None:
            return None, None

        l1 = self._read_dbus(f"{self._grid_service}/Ac/L1/Voltage")
        l2 = self._read_dbus(f"{self._grid_service}/Ac/L2/Voltage")

        if l1 is not None:
            self._voltage_l1 = l1
        if l2 is not None:
            self._voltage_l2 = l2

        return self._voltage_l1, self._voltage_l2

    def _discover_grid_service(self) -> str | None:
        """Find first com.victronenergy.grid.* service via dbus-send."""
        try:
            result = subprocess.run(
                [
                    "dbus-send",
                    "--system",
                    "--print-reply",
                    "--dest=org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus.ListNames",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode != 0:
                return None

            matches = re.findall(r'"(com\.victronenergy\.grid[^"]*)"', result.stdout)
            for svc in matches:
                # verify it has the voltage paths
                v = self._read_dbus(f"{svc}/Ac/L1/Voltage", silent=True)
                if v is not None and v > 0:
                    logger.info("Grid voltage: found %s (L1=%.1fV)", svc, v)
                    return svc
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.debug("Grid service discovery failed: %s", e)
        return None

    def _read_dbus(self, path: str, silent: bool = False) -> float | None:
        """Read a single D-Bus path via dbus-send."""
        try:
            result = subprocess.run(
                [
                    "dbus-send",
                    "--system",
                    "--print-reply",
                    f"--dest={path.split('/')[0]}",
                    path,
                    "com.victronenergy.BusItem.GetValue",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode != 0:
                return None

            # variant: double variant: <float64 234.5> or <double 123.82>
            m = re.search(r"(?:float64|double)\s+([\d.]+)", result.stdout)
            if m:
                return float(m.group(1))

            # variant: variant <int32 234>
            m = re.search(r"variant\s+<int32\s+(\d+)>", result.stdout)
            if m:
                return float(m.group(1))

            if not silent:
                logger.debug(
                    "No float64/int32 in dbus-send output for %s: %s", path, result.stdout[:200]
                )
            return None
        except (OSError, subprocess.TimeoutExpired) as e:
            if not silent:
                logger.debug("dbus-send read failed for %s: %s", path, e)
            return None
