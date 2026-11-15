"""Device: probe, cache, subscriptions, debounce, typed setters."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.airstage_direct.api import objects
from custom_components.airstage_direct.api.codecs import CodecError, FanSpeed, Missing, OpMode
from custom_components.airstage_direct.api.device import Capabilities, Device
from custom_components.airstage_direct.api.transport import AirstageWriteError, Transport
from tests.simulator import Simulator


@pytest.fixture
def device(transport: Transport) -> Device:
    return Device(transport)


async def test_probe_classifies_every_object(device: Device, sim: Simulator) -> None:
    sim.indoor["iu_indoor_tmp"] = "65535"  # as right after power-on
    caps = await device.probe()
    assert caps.unknown == objects.KNOWN_UNKNOWN_L45
    assert "iu_has_ai_eco" in caps.unsupported
    assert "iu_indoor_tmp" in caps.unsupported
    assert "ou_cmp_freq" in caps.supported
    assert caps.limits[OpMode.HEAT] == (16.0, 30.0)
    assert caps.limits[OpMode.COOL] == (18.0, 30.0)
    assert caps.firmware == sim.indoor["iu_main_ver"]
    assert device.transport.gap == 0.1  # restored after the faster setup pace
    assert sim.overlaps == 0
    assert not device.supports("iu_has_watch")
    assert device.supports("iu_set_tmp")


def test_capabilities_round_trip() -> None:
    caps = Capabilities(
        supported={"a"},
        unsupported={"b"},
        unknown={"c"},
        unknown_adapter={"wla_info"},
        refused={"ou_low_noise"},
        limits={OpMode.HEAT: (16.0, 30.0)},
        firmware="v1",
    )
    again = Capabilities.from_dict(caps.to_dict())
    assert again == caps


async def test_refresh_reads_only_subscribed_supported_names(
    device: Device, sim: Simulator
) -> None:
    device.capabilities = Capabilities(unsupported={"iu_has_ai_eco"}, unknown={"iu_has_watch"})
    undo = device.subscribe(["iu_set_tmp", "ou_cmp_freq", "iu_has_ai_eco", "iu_has_watch"])
    changed = await device.refresh()
    assert changed == {"iu_set_tmp", "ou_cmp_freq"}
    sent = {n for lst in sim.get_lists() for n in lst}
    assert "iu_has_ai_eco" not in sent
    assert "iu_has_watch" not in sent
    assert device.value("iu_set_tmp") == 22.0
    undo()
    assert device.subscribed == set()
    sim.requests.clear()
    assert await device.refresh() == set()
    assert sim.requests == []


async def test_subscriptions_are_counted(device: Device) -> None:
    undo_a = device.subscribe(["iu_set_tmp"])
    undo_b = device.subscribe(["iu_set_tmp"])
    undo_a()
    assert device.subscribed == {"iu_set_tmp"}
    undo_b()
    assert device.subscribed == set()


def test_empty_value_is_held_for_one_update(device: Device) -> None:
    device.apply({"ou_cmp_freq": "34"})
    assert device.apply({"ou_cmp_freq": ""}) == set()
    assert device.value("ou_cmp_freq") == 34
    assert device.apply({"ou_cmp_freq": ""}) == {"ou_cmp_freq"}
    assert device.value("ou_cmp_freq") is Missing.EMPTY
    assert device.apply({"ou_cmp_freq": "30"}) == {"ou_cmp_freq"}
    assert device.apply({"ou_cmp_freq": ""}) == set()  # held again after a real value


def test_apply_decodes_and_notifies(device: Device) -> None:
    seen: list[set[str]] = []
    undo = device.add_listener(seen.append)
    device.apply({"iu_op_mode": "4", "iu_fan_spd": "8", "ou_heat_ex_tmp_out": "-1400"})
    assert device.value("iu_op_mode") is OpMode.HEAT
    assert device.value("iu_fan_spd") is FanSpeed.MEDIUM
    assert device.value("ou_heat_ex_tmp_out") == -14.0
    device.apply({"iu_op_mode": "4"})  # no change, no call
    device.apply({"iu_fan_spd": "99"})  # undecodable: kept raw, not dropped
    assert device.value("iu_fan_spd") == "99"
    device.apply({"wla_rssi": -52}, sub_id=1)
    assert device.value("wla_rssi") == -52
    undo()
    device.apply({"iu_op_mode": "1"})
    assert len(seen) == 3


async def test_writes_within_300_ms_go_out_together(device: Device, sim: Simulator) -> None:
    device.apply({"iu_op_mode": "4"})
    await asyncio.gather(
        device.set_fan_speed(FanSpeed.LOW),
        device.set_setpoint(21.5),
        device.set_mode(OpMode.HEAT),
        device.set_power(True),
    )
    writes = [r["body"]["value"] for r in sim.requests if r["endpoint"] == "SetParam"]
    assert writes == [  # one name each, in a fixed order
        {"iu_onoff": "1"},
        {"iu_op_mode": "4"},
        {"iu_set_tmp": "215"},
        {"iu_fan_spd": "5"},
    ]
    assert device.value("iu_set_tmp") == 21.5
    assert sim.indoor["iu_fan_spd"] == "5"


async def test_other_setters(device: Device, sim: Simulator) -> None:
    await device.set_vane_vertical(2)
    await device.set_vane_horizontal(3)
    await device.set_swing_vertical(True)
    await device.set_swing_horizontal(False)
    await device.set_switch("iu_economy", True)
    assert sim.indoor["iu_af_dir_vrt"] == "2"
    assert sim.indoor["iu_economy"] == "1"
    with pytest.raises(ValueError, match="not a writable on/off"):
        await device.set_switch("iu_set_tmp", True)


async def test_setpoint_limits_follow_the_mode(device: Device, sim: Simulator) -> None:
    await device.probe()
    device.apply({"iu_op_mode": "4"})  # heat: 16..30
    await device.set_setpoint(16.0)
    with pytest.raises(CodecError):
        await device.set_setpoint(15.5)
    device.apply({"iu_op_mode": "1"})  # cool: 18..30
    with pytest.raises(CodecError):
        await device.set_setpoint(17.0)
    with pytest.raises(CodecError):
        await device.set_setpoint(21.3)  # not a 0.5 step


async def test_pending_mode_decides_the_limits(device: Device, sim: Simulator) -> None:
    await device.probe()
    device.apply({"iu_op_mode": "4"})
    results = await asyncio.gather(
        device.set_mode(OpMode.COOL), device.set_setpoint(17.0), return_exceptions=True
    )
    assert results[0] is None
    assert isinstance(results[1], CodecError)


async def test_write_refused_reaches_the_caller(device: Device, sim: Simulator) -> None:
    sim.write_res = "unknown"
    with pytest.raises(AirstageWriteError):
        await device.set_fan_speed(FanSpeed.HIGH)
    assert device.value("iu_fan_spd") is None  # cache not updated


async def test_refusals_are_remembered(device: Device, sim: Simulator) -> None:
    """Outdoor low noise: a multi-split refuses it, a single split takes it."""
    sim.write_res = "unknown"
    with pytest.raises(AirstageWriteError):
        await device.set_switch("ou_low_noise", True)
    assert device.refused == {"ou_low_noise"}
    sim.write_res = "nack"  # a rejection, not a refusal of the object
    with pytest.raises(AirstageWriteError):
        await device.set_switch("iu_economy", True)
    assert device.refused == {"ou_low_noise"}
    sim.write_res = "ack"
    await device.set_switch("ou_low_noise", True)
    assert device.refused == set()
    assert sim.indoor["ou_low_noise"] == "1"


async def test_unsupported_object_is_not_written(device: Device, sim: Simulator) -> None:
    device.capabilities = Capabilities(unsupported={"iu_af_dir_hrz"})
    with pytest.raises(ValueError, match="does not support"):
        await device.set_vane_horizontal(3)
    with pytest.raises(ValueError, match="not writable"):
        await device._write("iu_indoor_tmp", 21.0)
    assert sim.requests == []


async def test_recheck_promotes_objects_that_now_answer(device: Device, sim: Simulator) -> None:
    sim.indoor["iu_indoor_tmp"] = "65535"  # right after power-on
    await device.probe()
    assert not device.supports("iu_indoor_tmp")
    sim.indoor["iu_indoor_tmp"] = "7250"
    sim.requests.clear()
    assert await device.recheck_unsupported() == {"iu_indoor_tmp"}
    assert device.supports("iu_indoor_tmp")
    assert device.value("iu_indoor_tmp") == 22.5
    assert "iu_has_ai_eco" in device.capabilities.unsupported  # type: ignore[union-attr]
    assert all(n not in objects.KNOWN_UNKNOWN_L45 for lst in sim.get_lists() for n in lst)
    device.capabilities = None
    assert await device.recheck_unsupported() == set()


async def test_adapter_values_are_read_on_sub_id_1(device: Device, sim: Simulator) -> None:
    device.capabilities = Capabilities()
    device.subscribe(["wla_rssi", "wla_info"], sub_id=1)
    device.subscribe(["iu_set_tmp"])
    assert device.subscribed == {"iu_set_tmp"}
    assert device.subscribed_for(1) == {"wla_rssi", "wla_info"}
    await device.refresh(sub_id=1)
    assert device.value("wla_rssi") == sim.adapter["wla_rssi"]
    assert all(r["body"]["device_sub_id"] == 1 for r in sim.requests)
    assert device.capabilities.unknown_adapter == {"wla_info"}  # remembered...
    assert "wla_info" not in device.capabilities.unknown  # ...not as an indoor name
    assert device.subscribed_for(1) == {"wla_rssi"}


async def test_subscription_hooks(device: Device) -> None:
    calls: list[int] = []
    remove = device.on_subscriptions_changed(lambda: calls.append(1))
    undo = device.subscribe(["iu_set_tmp"])
    undo()
    remove()
    device.subscribe(["iu_fan_spd"])
    assert calls == [1, 1]


def test_refusals_follow_the_capabilities(device: Device) -> None:
    device.refused.add("ou_low_noise")
    caps = Capabilities(refused={"iu_wifi_led"})
    device.capabilities = caps
    assert device.refused == {"iu_wifi_led"}  # restored from the stored capabilities
