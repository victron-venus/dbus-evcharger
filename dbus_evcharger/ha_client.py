"""Home Assistant REST client for EV charger metrics.

Reads charger entities through one batched /api/template call. Returns
numeric values for charging current (A), power (W), energy (kWh), etc.
"""

import json
import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Jinja template sent to /api/template. Tokens are replaced literally
# (str.format would fight the Jinja braces).
TEMPLATE_BODY = """{{ {
  'status': states('@STATUS@') | string,
  'power': states('@POWER@') | float(0),
  'current': states('@CURRENT@') | float(0),
  'energy_forward': states('@ENERGY@') | float(0),
  'session_time': states('@SESSION_TIME@') | int(0),
  'startstop': states('@STARTSTOP@') | int(0),
  'setcurrent': states('@SETCURRENT@') | float(0)
} | to_json }}"""


class HomeAssistantError(Exception):
    """Base class for HA client errors."""


class HomeAssistantAPIError(HomeAssistantError):
    pass


class CircuitBreaker:
    """Opens after `threshold` consecutive failures; retries after reset_timeout s."""

    def __init__(self, threshold: int = 5, reset_timeout: float = 60.0) -> None:
        self.threshold = threshold
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_timeout:
            logger.info("Circuit breaker half-open, allowing retry")
            self._opened_at = None
            self._failures = self.threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._opened_at is None and self._failures >= self.threshold:
            self._opened_at = time.monotonic()
            logger.warning("Circuit breaker OPEN after %i consecutive failures", self._failures)


def build_template(
    status_entity: str,
    power_entity: str,
    current_entity: str,
    energy_entity: str,
    session_time_entity: str = "",
    startstop_entity: str = "",
    setcurrent_entity: str = "",
) -> str:
    return (
        TEMPLATE_BODY.replace("@STATUS@", status_entity)
        .replace("@POWER@", power_entity)
        .replace("@CURRENT@", current_entity)
        .replace("@ENERGY@", energy_entity)
        .replace("@SESSION_TIME@", session_time_entity or "__no_session_time__")
        .replace("@STARTSTOP@", startstop_entity or "__no_startstop__")
        .replace("@SETCURRENT@", setcurrent_entity or "__no_setcurrent__")
    )


class HaClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        status_entity: str,
        power_entity: str,
        current_entity: str,
        energy_entity: str,
        session_time_entity: str = "",
        startstop_entity: str = "",
        setcurrent_entity: str = "",
        timeout: float = 3.0,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.status_entity = status_entity
        self.power_entity = power_entity
        self.current_entity = current_entity
        self.energy_entity = energy_entity
        self.session_time_entity = session_time_entity
        self.startstop_entity = startstop_entity
        self.setcurrent_entity = setcurrent_entity
        self.timeout = timeout
        self.breaker = breaker or CircuitBreaker()
        self.last_known: dict[str, Any] = {
            "status": None,
            "power": None,
            "current": None,
            "energy_forward": None,
            "session_time": None,
            "startstop": None,
            "setcurrent": None,
        }
        self._template = build_template(
            status_entity,
            power_entity,
            current_entity,
            energy_entity,
            session_time_entity,
            startstop_entity,
            setcurrent_entity,
        )
        self._configured = all((base_url, token, status_entity))
        self._session = requests.Session()
        if token:
            self._session.headers.update(
                {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            )
        self._last_error_log = 0.0

    def _log_error_throttled(self, msg: str) -> None:
        now = time.monotonic()
        if now - self._last_error_log >= 60.0:
            self._last_error_log = now
            logger.error(msg)

    def poll(self) -> dict[str, Any]:
        """Fetch charger metrics.

        Returns dict with numeric fields: status, power, current,
        energy_forward, session_time, startstop, setcurrent, and ok=True when
        the call succeeded. On failure, last-known is returned with ok=False.
        """
        result = dict(self.last_known)
        result["ok"] = False
        if not self._configured:
            self._log_error_throttled("HA client not configured (local_config.py missing?)")
            return result
        if self.breaker.is_open:
            return result
        try:
            resp = self._session.post(
                f"{self.base_url}/api/template",
                json={"template": self._template},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                raise HomeAssistantAPIError(f"/api/template HTTP {resp.status_code}")
            data = json.loads(resp.text)
            status_raw = str(data.get("status", "")).strip()
            power_raw = str(data.get("power", "0")).strip()
            current_raw = str(data.get("current", "0")).strip()
            energy_raw = str(data.get("energy_forward", "0")).strip()
            session_time_raw = str(data.get("session_time", "0")).strip()
            startstop_raw = str(data.get("startstop", "0")).strip()
            setcurrent_raw = str(data.get("setcurrent", "0")).strip()

            # Coerce numerics with safe fallbacks
            def _f(s: str) -> float | None:
                try:
                    return float(s)
                except ValueError:
                    return None

            def _i(s: str) -> int | None:
                try:
                    return int(float(s))
                except ValueError:
                    return None

            result.update(
                status=status_raw if status_raw else None,
                power=_f(power_raw),
                current=_f(current_raw),
                energy_forward=_f(energy_raw),
                session_time=_i(session_time_raw),
                startstop=_i(startstop_raw),
                setcurrent=_f(setcurrent_raw),
                ok=True,
            )
            self.last_known = {k: result[k] for k in self.last_known}
            self.breaker.record_success()
        except requests.exceptions.Timeout as exc:
            self.breaker.record_failure()
            self._log_error_throttled(f"HA timeout: {exc}")
        except requests.exceptions.RequestException as exc:
            self.breaker.record_failure()
            self._log_error_throttled(f"HA connection error: {exc}")
        except HomeAssistantError as exc:
            self.breaker.record_failure()
            self._log_error_throttled(str(exc))
        except ValueError as exc:
            self.breaker.record_failure()
            self._log_error_throttled(f"HA template returned invalid JSON: {exc}")
        return result
