# Changelog

All notable changes to this project will be documented in this file.

## [0.1.4] - 2026-09-12

### Fixed
- Defer native D-Bus name registration until all mandatory and device paths, configured initial values, and callbacks are ready. Register each private-bus service once.
- Publish the production service as disconnected until a valid source snapshot arrives; preserve control defaults and existing freshness deadlines.
- Verify native registration order, configured values, idempotence and failure before publication with isolated fake-bus regressions.

## [0.1.3] - 2026-09-12

### Fixed
- Move blocking HA and grid-voltage reads to one coalescing worker so the D-Bus main loop stays responsive during slow requests. Deliver results on the main loop, retain acquisition timestamps, and discard callbacks after shutdown.
- Keep one reconnecting MQTT client and expire each field independently after five seconds. Reconnect, unrelated topics and delayed delivery cannot renew old status or power.
- Publish worker results immediately and check MQTT freshness at least once per second without accelerating the configured HA/grid polling interval.
- Treat unavailable, unknown and non-finite source values as invalid. Clear stale measurements instead of publishing zero, and clear cached voltage after source loss before rediscovery.
- Derive each phase's current from its own power using the configured phase count. Preserve explicit zero readings, measured phase currents and invalid optional values.

### Documentation
- Describe actual freshness deadlines, worker shutdown and the current telemetry-only control mirroring. Physical charger actuation and broker/network failure drills remain separate validation boundaries.

## [Unreleased]

### Changed
- Default `POLL_INTERVAL` 2.0s → 15.0s.

### Added
- `DEVICE_INSTANCE` config → `/DeviceInstance` on the evcharger service
- All standard VE.Dbus evcharger paths registered:
  - `/Status`, `/Ac/Power`, `/Ac/Energy/Forward`, `/Ac/L1/Power`, `/Ac/L1/Voltage`,
    `/Ac/L1/Current`, `/Ac/L1/PowerFactor`, `/Current`, `/SetCurrent`, `/Mode`,
    `/StartStop`, `/Position`, `/MinCurrent`, `/MaxCurrent`, `/AutoStart`,
    `/Session/Time`, `/Session/Energy`, `/Session/Cost`, `/Session/UserId`,
    `/Session/UserIdType`
- Alarms and warnings paths (`/Alarms/*`, `/Warnings/*`)
- HA entity config for all EV charger metrics (status, power, current, energy, session time, startstop, setcurrent)
- MQTT fallback config (host, port, username, password, topic, qos)
- Polling interval and HA timeout config
- Default device defaults (position, max/min current, phases, custom name)
- `NullDbusService` for off-GX testing
- `ha_configured()` and `mqtt_configured()` helper functions
- Session tracking (/Session/Time, /Session/Energy)
- Control flow: HA primary, MQTT fallback, stale data retention
- Deployment scripts: deploy.sh / update.sh / restart.sh; daemontools unit with multilog
- CI via venus-os-ci-toolkit python workflow

## [Unreleased]

### Documentation
- README: full EV charger data-flow diagram (HA sensors → dbus-evcharger → D-Bus → Cerbo MQTT topics → consumers) and control flow diagram; consumer table
- local_config.example.py updated with EV charger defaults

## [0.1.1] - 2026-08-29

### Changed
- Version bump to 0.1.1 (Venus `version` file / package).

## [0.1.0] - 2026-08-29

### Added
- HA-backed EV charger D-Bus bridge for Venus OS.
- All standard VE.Dbus evcharger paths from the Venus OS wiki.
- HA REST client with circuit breaker (5 failures → 60 s open), last-known values while unreachable, once/min error throttle.
- MQTT fallback client with lazy paho-mqtt import for Venus OS compatibility.
- Session tracking (time + energy).
- Control: /Mode, /StartStop, /SetCurrent writable to HA/MQTT.
- Deployment: deploy.sh / update.sh / restart.sh trio; daemontools unit with multilog.
- CI via venus-os-ci-toolkit python workflow + codeql + dependency-review + auto-merge + auto-approve + python-security + trivy-fs + scorecard.

### Verified on device (Cerbo GX, Venus v3.75)
- All evcharger paths register; status live on MQTT (`N/<portal>/evcharger/40/Status`).
- Failure drill: HA unreachable → circuit breaker opens, `/Status` fault, `/Connected` 0; auto-recovery after restore.

### Known limitations
- Round-trip control drill pending EV charger hardware online.
- MQTT QoS 1 at-least-once delivery semantics may reorder messages under network loss.
