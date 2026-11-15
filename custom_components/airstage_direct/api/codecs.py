"""Raw adapter values to Python values and back.

Every raw value is first checked for the two sentinels: "65535" (not supported,
or no value yet right after power-on) and "" (no value right now, e.g. during
a monitor refresh). Only then is it scaled, so 65535 can never become 655.35 C.
Raw values may be strings (indoor objects, pushes) or numbers (wla_* values in
GetParam answers).
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Final

from .objects import Codec

Raw = str | int | float


class Missing(Enum):
    """A raw value that carries no reading."""

    EMPTY = "empty"  # ""
    UNSUPPORTED = "unsupported"  # "65535"


class OpMode(IntEnum):
    AUTO = 0
    COOL = 1
    DRY = 2
    FAN = 3
    HEAT = 4


class FanSpeed(IntEnum):
    """iu_fan_spd codes, measured against the app's labels."""

    AUTO = 0
    QUIET = 2
    LOW = 5
    LOW_MEDIUM = 7
    MEDIUM = 8
    MEDIUM_HIGH = 9
    HIGH = 11


Value = float | int | bool | str | OpMode | FanSpeed | Missing

UNSUPPORTED_RAW: Final = "65535"
TEMP_OFFSET: Final = 5000
VANE_VERTICAL_POSITIONS: Final = range(1, 5)
VANE_HORIZONTAL_POSITIONS: Final = range(1, 6)
SETPOINT_STEP: Final = 0.5
SETPOINT_RANGE: Final = (10.0, 32.0)  # outer bounds; the unit's own limits are checked too


class CodecError(ValueError):
    """A raw value could not be decoded, or a value cannot be written."""


def sentinel(raw: Raw) -> Missing | None:
    text = str(raw).strip()
    if text == "":
        return Missing.EMPTY
    if text == UNSUPPORTED_RAW:
        return Missing.UNSUPPORTED
    return None


def _int(raw: Raw) -> int:
    try:
        if isinstance(raw, float):
            if not raw.is_integer():
                raise CodecError(f"not an integer: {raw!r}")
            return int(raw)
        return int(str(raw).strip())
    except ValueError as err:
        raise CodecError(f"not an integer: {raw!r}") from err


def decode(codec: Codec, raw: Raw) -> Value:
    """Decode a raw value. Unknown or invalid numbers raise CodecError."""
    if (missing := sentinel(raw)) is not None:
        return missing
    match codec:
        case Codec.RAW | Codec.TEXT:
            return raw if isinstance(raw, str) else str(raw)
        case Codec.INT | Codec.FREQ | Codec.RPM:
            return _int(raw)
        case Codec.TEMP_100:
            return _int(raw) / 100
        case Codec.TEMP_OFFSET:
            return (_int(raw) - TEMP_OFFSET) / 100
        case Codec.TEMP_10:
            return _int(raw) / 10
        case Codec.CURRENT_100:
            return _int(raw) / 100
        case Codec.BOOL:
            value = _int(raw)
            if value not in (0, 1):
                raise CodecError(f"not 0/1: {raw!r}")
            return value == 1
        case Codec.OP_MODE:
            try:
                return OpMode(_int(raw))
            except ValueError as err:
                raise CodecError(f"unknown mode code {raw!r}") from err
        case Codec.FAN_SPEED:
            try:
                return FanSpeed(_int(raw))
            except ValueError as err:
                raise CodecError(f"unknown fan code {raw!r}") from err
        case Codec.VANE_VERTICAL:
            return _position(raw, VANE_VERTICAL_POSITIONS)
        case Codec.VANE_HORIZONTAL:
            return _position(raw, VANE_HORIZONTAL_POSITIONS)
    raise CodecError(f"no decoder for {codec}")  # pragma: no cover


def _position(raw: Raw, positions: range) -> int:
    value = _int(raw)
    if value not in positions:
        raise CodecError(f"position {raw!r} outside {positions.start}..{positions.stop - 1}")
    return value


def encode(codec: Codec, value: Value) -> str:
    """Encode a value for SetParam. Only codecs with a known encoding can be written."""
    if isinstance(value, Missing):
        raise CodecError("cannot write a missing value")
    match codec:
        case Codec.BOOL:
            if not isinstance(value, bool):
                raise CodecError(f"expected a bool, got {value!r}")
            return "1" if value else "0"
        case Codec.TEMP_10:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise CodecError(f"expected a temperature, got {value!r}")
            return str(encode_setpoint(float(value)))
        case Codec.OP_MODE:
            return str(int(_member(OpMode, value)))
        case Codec.FAN_SPEED:
            return str(int(_member(FanSpeed, value)))
        case Codec.VANE_VERTICAL:
            return str(_position_value(value, VANE_VERTICAL_POSITIONS))
        case Codec.VANE_HORIZONTAL:
            return str(_position_value(value, VANE_HORIZONTAL_POSITIONS))
    raise CodecError(f"{codec} cannot be written")


def encode_setpoint(celsius: float) -> int:
    """Setpoint in 0.5 C steps, as tenths of a degree. The unit's own limits are
    checked by the device; this only rejects values that are never valid."""
    lo, hi = SETPOINT_RANGE
    if not lo <= celsius <= hi:
        raise CodecError(f"setpoint {celsius} outside {lo}..{hi}")
    halves = celsius / SETPOINT_STEP
    if abs(halves - round(halves)) > 1e-9:
        raise CodecError(f"setpoint {celsius} is not a multiple of {SETPOINT_STEP}")
    return round(round(halves) * SETPOINT_STEP * 10)


def _member[E: IntEnum](enum: type[E], value: Value) -> E:
    if isinstance(value, enum):
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodecError(f"expected {enum.__name__}, got {value!r}")
    try:
        return enum(value)
    except ValueError as err:
        raise CodecError(f"{value!r} is not a valid {enum.__name__}") from err


def _position_value(value: Value, positions: range) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodecError(f"expected a position, got {value!r}")
    if value not in positions:
        raise CodecError(f"position {value} outside {positions.start}..{positions.stop - 1}")
    return value
