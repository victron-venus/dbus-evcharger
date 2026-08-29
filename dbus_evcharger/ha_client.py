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
# (str.format would fight the Jinja braces). Optional entities omitted
# when empty -> emit null via conditional Jinja.
TEMPLATE_BODY = """{{ {
  'status': states('@STATUS@') | string,
  'power': {% if '@POWER@' != '' %}states('@POWER@') | float(0){% else %}none{% endif %},
  'current': {% if '@CURRENT@' != '' %}states('@CURRENT@') | float(0){% else %}none{% endif %},
  'energy_forward': {% if '@ENERGY@' != '' %}states('@ENERGY@') | float(0){% else %}none{% endif %},
  'session_time': {% if '@SESSION_TIME@' != '' %}states('@SESSION_TIME@') | int(0){% else %}none{% endif %},
  'startstop': {% if '@STARTSTOP@' != '' %}states('@STARTSTOP@') | int(0){% else %}none{% endif %},
  'setcurrent': {% if '@SETCURRENT@' != '' %}states('@SETCURRENT@') | float(0){% else %}none{% endif %}
} | to_json }}"""


class HomeAssistantError(Exception):
    """Base class for HA client errors."""


class HomeAssistantAPIError(HomeAssistantError):
    pass


def _str_or_none(s: Any) -> str | None:
    """Coerce a value to a trimmed string, returning None for empty/none/unavailable."""
    if s is None:
        return None
    s = str(s).strip()
    if s == "" or s.lower() in ("none", "unknown", "unavailable"):
        return None
    return s


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
        .replace("@POWER@", power_entity or "")
        .replace("@CURRENT@", current_entity or "")
        .replace("@ENERGY@", energy_entity or "")
        .replace("@SESSION_TIME@", session_time_entity or "")
        .replace("@STARTSTOP@", startstop_entity or "")
        .replace("@SETCURRENT@", setcurrent_entity or "")
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
        self._configured = bool(base_url and token)
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
            self._log_error_throttled("HA client not configured (HA_URL or HA_TOKEN empty)")
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

            def _f(s: Any) -> float | None:
                if s is None:
                    return None
                s = str(s).strip()
                if s == "" or s.lower() in ("none", "unknown", "unavailable"):
                    return None
                try:
                    return float(s)
                except ValueError:
                    return None

            def _i(s: Any) -> int | None:
                if s is None:
                    return None
                s = str(s).strip()
                if s == "" or s.lower() in ("none", "unknown", "unavailable"):
                    return None
                try:
                    return int(float(s))
                except ValueError:
                    return None

            result.update(
                status=_str_or_none(data.get("status")),
                power=_f(data.get("power")),
                current=_f(data.get("current")),
                energy_forward=_f(data.get("energy_forward")),
                session_time=_i(data.get("session_time")),
                startstop=_i(data.get("startstop")),
                setcurrent=_f(data.get("setcurrent")),
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
