"""EV charger control constants and helpers.

The actual control logic lives in the D-Bus service callbacks (on_mode,
on_startstop, on_setcurrent) which forward writes from Venus OS / VRM to
HA/MQTT. This module only provides the mode constants.
"""

# /Mode values (matches Victron EVCS firmware)
MODE_MANUAL = 0
MODE_AUTO = 1
MODE_SCHEDULED = 2

# /StartStop values
STARTSTOP_STOP = 0
STARTSTOP_START = 1

# /Position values
POSITION_AC_OUTPUT = 0  # charger draws from AC output (grid/inverter)
POSITION_AC_INPUT = 1  # charger feeds back (V2G / vehicle-to-home)
