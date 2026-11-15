"""Codecs: sentinels first, then scaling; checked against the recorded answers."""

from __future__ import annotations

import pytest

from custom_components.airstage_direct.api import codecs, objects
from custom_components.airstage_direct.api.codecs import (
    CodecError,
    FanSpeed,
    Missing,
    OpMode,
    decode,
    encode,
)
from custom_components.airstage_direct.api.objects import Codec
from tests.simulator import load_fixture

ALL_CODECS = list(Codec)


@pytest.mark.parametrize("codec", ALL_CODECS)
def test_sentinels_come_before_scaling(codec: Codec) -> None:
    assert decode(codec, "65535") is Missing.UNSUPPORTED
    assert decode(codec, 65535) is Missing.UNSUPPORTED
    assert decode(codec, "") is Missing.EMPTY
    assert decode(codec, " ") is Missing.EMPTY


@pytest.mark.parametrize(
    ("codec", "raw", "expected"),
    [
        (Codec.TEMP_100, "2525", 25.25),
        (Codec.TEMP_100, "-1400", -14.0),
        (Codec.TEMP_OFFSET, "7200", 22.0),
        (Codec.TEMP_OFFSET, "5300", 3.0),
        (Codec.TEMP_OFFSET, "4900", -1.0),
        (Codec.TEMP_10, "220", 22.0),
        (Codec.TEMP_10, "225", 22.5),
        (Codec.CURRENT_100, "570", 5.7),
        (Codec.FREQ, "52", 52),
        (Codec.RPM, "960", 960),
        (Codec.INT, -52, -52),
        (Codec.INT, "-52", -52),
        (Codec.INT, 3.0, 3),
        (Codec.BOOL, "1", True),
        (Codec.BOOL, "0", False),
        (Codec.OP_MODE, "4", OpMode.HEAT),
        (Codec.FAN_SPEED, "7", FanSpeed.LOW_MEDIUM),
        (Codec.FAN_SPEED, "11", FanSpeed.HIGH),
        (Codec.VANE_VERTICAL, "4", 4),
        (Codec.VANE_HORIZONTAL, "5", 5),
        (Codec.TEXT, "E101V07P00L02-1", "E101V07P00L02-1"),
        (Codec.RAW, 3987155060, "3987155060"),
        (Codec.RAW, "----/--/--,-,-", "----/--/--,-,-"),
    ],
)
def test_decode(codec: Codec, raw: codecs.Raw, expected: codecs.Value) -> None:
    assert decode(codec, raw) == expected


@pytest.mark.parametrize(
    ("codec", "raw"),
    [
        (Codec.TEMP_100, "abc"),
        (Codec.INT, 3.5),
        (Codec.BOOL, "2"),
        (Codec.OP_MODE, "9"),
        (Codec.FAN_SPEED, "3"),
        (Codec.VANE_VERTICAL, "5"),
        (Codec.VANE_HORIZONTAL, "0"),
    ],
)
def test_decode_rejects(codec: Codec, raw: codecs.Raw) -> None:
    with pytest.raises(CodecError):
        decode(codec, raw)


@pytest.mark.parametrize(
    ("codec", "value", "raw"),
    [
        (Codec.BOOL, True, "1"),
        (Codec.BOOL, False, "0"),
        (Codec.TEMP_10, 22.5, "225"),
        (Codec.TEMP_10, 18, "180"),
        (Codec.OP_MODE, OpMode.COOL, "1"),
        (Codec.OP_MODE, 4, "4"),
        (Codec.FAN_SPEED, FanSpeed.QUIET, "2"),
        (Codec.VANE_VERTICAL, 1, "1"),
        (Codec.VANE_HORIZONTAL, 3, "3"),
    ],
)
def test_encode(codec: Codec, value: codecs.Value, raw: str) -> None:
    assert encode(codec, value) == raw
    assert decode(codec, raw) == value


@pytest.mark.parametrize(
    ("codec", "value"),
    [
        (Codec.BOOL, 1),
        (Codec.TEMP_10, 22.3),
        (Codec.TEMP_10, 40.0),
        (Codec.TEMP_10, True),
        (Codec.TEMP_10, "22"),
        (Codec.OP_MODE, 7),
        (Codec.OP_MODE, True),
        (Codec.FAN_SPEED, 3),
        (Codec.VANE_VERTICAL, 5),
        (Codec.VANE_HORIZONTAL, True),
        (Codec.TEMP_100, 21.0),
        (Codec.RAW, "x"),
        (Codec.BOOL, Missing.EMPTY),
    ],
)
def test_encode_rejects(codec: Codec, value: codecs.Value) -> None:
    with pytest.raises(CodecError):
        encode(codec, value)


@pytest.mark.parametrize("fixture", ["unit_downstairs_14kj.json", "unit_upstairs_07kj.json"])
def test_every_recorded_value_decodes(fixture: str) -> None:
    """The codec assigned to each object fits what the real units answered."""
    data = load_fixture(fixture)
    for sub_id, part in ((0, "indoor"), (1, "adapter")):
        for name, raw in data[part].items():
            obj = objects.get(name, sub_id)
            assert obj is not None, f"{name} answered but is not in the registry"
            decode(obj.codec, raw)  # must not raise
