"""Tests for EV charger control constants."""

from dbus_evcharger.control import (
    MODE_AUTO,
    MODE_MANUAL,
    MODE_SCHEDULED,
    POSITION_AC_INPUT,
    POSITION_AC_OUTPUT,
    STARTSTOP_START,
    STARTSTOP_STOP,
)


def test_mode_constants_match_victron():
    assert (MODE_MANUAL, MODE_AUTO, MODE_SCHEDULED) == (0, 1, 2)


def test_startstop_constants():
    assert (STARTSTOP_STOP, STARTSTOP_START) == (0, 1)


def test_position_constants():
    assert (POSITION_AC_OUTPUT, POSITION_AC_INPUT) == (0, 1)
