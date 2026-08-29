# dbus-evcharger deployment notes

Findings from the first bring-up (2026-08-29), Cerbo GX on Raspberry Pi 3,
Venus OS v3.75, portal ID `b827ebea1ece`.

## Device facts

- Services run under daemontools at `/data/dbus-evcharger`, symlinked from
  `/service/dbus-evcharger`; logs in `/var/log/dbus-evcharger` via multilog.
- Deploy path: `./deploy.sh` from a dev machine (rsync + on-device
  `update.sh`), or the auto-deploy webhook from inverter-monitoring.
- Default D-Bus instance is 40: `com.victronenergy.evcharger.40`.
- All D-Bus paths follow the VE.Dbus evcharger specification
  (https://github.com/victronenergy/venus/wiki/dbus#evcharger).

## vedbus / Venus gotchas hit during bring-up

| Symptom | Cause | Fix |
|---|---|---|
| `Invalid bus name 'com.victronenergy.evcharger.40'` | bus names forbid digits after the last dot | text suffix (`evcharger.40`); instance only in `/DeviceInstance` |
| `KeyError: Can't register object-path '/'` on 2nd service | VeDbusService defaults to the shared bus and exports `/` | one `dbus.SystemBus(private=True)` per service |
| `add_path() unexpected kwarg 'onchange'` | vedbus API is `onchangecallback=path,value` | aligned mock + real signature |
| `'VeDbusService' has no attribute 'items'` | it's not a dict | mirror values as instance attributes |
| empty multilog | stderr not captured | `exec 2>&1` in service run script |
| crash-loop after update: stale supervise/run processes with `(deleted)` cwd | daemontools inode churn across updates | `update.sh` reaps by `/proc/*/cwd` scan before reinstall |

## Verification results

- MQTT dump on device: `N/b827ebea1ece/evcharger/40/Status {"value":2}`,
  `evcharger/40/Ac/Power {"value":3500}`, `evcharger/40/Mode {"value":1}` ✓
- Failure drills: HA down → circuit breaker opens after 5 consecutive
  failures, `/Status=0`, `/Connected=0`; HA back → auto recovery ✓
- Round-trip control deferred until HA entities are online again.

## Desktop consumer (inverter-desktop)

EV charger section is Cerbo-MQTT-first: subscribes
`N/<portal>/evcharger/40/...` for all standard paths. Falls back to direct
HA entities only when no MQTT data arrives. Toggles from the desktop still
route via HA REST (unchanged); single-control-plane writes over
`W/...//Mode` were evaluated and skipped.

## Residual risks

- Cerbo power loss while charging: no hardware interlock to stop the charger.
- Two writers to HA switch/number entities (desktop toggle vs automation) are
  last-write-wins by design.