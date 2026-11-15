"""The object registry: names, ids, groups and the write allow-list."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from custom_components.airstage_direct.api import objects
from custom_components.airstage_direct.api.objects import Group

API_DIR = Path(objects.__file__).parent


def test_registry_matches_the_dump() -> None:
    named = [o for o in objects.INDOOR if o.group is not Group.SPECIAL]
    # 148 objects in the dump: 4 unnamed and 2 never touched are not listed.
    assert len(named) + 4 + len(objects.NEVER_TOUCH) == 148
    names = [(o.sub_id, o.name) for o in objects.INDOOR + objects.ADAPTER]
    assert len(names) == len(set(names))
    assert not objects.NEVER_TOUCH & {o.name for o in objects.INDOOR}


def test_write_allow_list() -> None:
    assert {
        "iu_onoff", "iu_op_mode", "iu_set_tmp", "iu_fan_spd",
        "iu_af_dir_vrt", "iu_af_swg_vrt", "iu_af_dir_hrz", "iu_af_swg_hrz",
        "iu_economy", "iu_min_heat", "iu_fan_ctrl", "iu_powerful", "ou_low_noise",
        "iu_wifi_led", "iu_hmn_det_auto_save",
    } == objects.WRITABLE  # fmt: skip
    for read_only in ("iu_indoor_tmp", "iu_demand"):  # measured value, status
        assert read_only not in objects.WRITABLE
    for obj in objects.INDOOR:
        if obj.writable:
            assert obj.codec is not objects.Codec.RAW, obj.name
            assert obj.sub_id == 0


def test_lookup_and_function_settings() -> None:
    assert objects.get("ou_cmp_freq") is not None
    assert objects.get("wla_rssi", 1) is not None
    assert objects.get("wla_rssi", 0) is None
    assert objects.function_setting_name(5) == "iu_fld_set05"
    with pytest.raises(ValueError, match="numbered"):
        objects.function_setting_name(100)


def test_monitor_names() -> None:
    assert "ou_cmp_freq" in objects.MONITOR
    assert "iu_outdoor_tmp" not in objects.MONITOR
    assert len(objects.MONITOR) == 30


def test_api_does_not_import_home_assistant() -> None:
    for path in API_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            assert not any(m.split(".")[0] == "homeassistant" for m in mods), path.name
