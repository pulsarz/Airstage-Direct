"""The adapter's named objects: id, group, codec, and whether we may write it.

Names and their protocol are described in docs/protocol.md; codecs and the
write allow-list come from measurements on real units (docs/measurements.md).
An object whose encoding is not known gets the RAW codec and is never writable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Group(StrEnum):
    CAPABILITY = "capability"
    STATE = "state"
    SETTINGS = "settings"
    INDOOR_MONITOR = "indoor_monitor"
    AI = "ai"
    POWER_MISC = "power_misc"
    OUTDOOR_MONITOR = "outdoor_monitor"
    VERSIONS = "versions"
    OTHER = "other"
    SPECIAL = "special"
    ADAPTER = "adapter"


class Codec(StrEnum):
    """How a raw value is read; see codecs.py."""

    RAW = "raw"  # unknown encoding: passed through unchanged
    INT = "int"  # plain integer, no known unit (counters, valve steps)
    TEMP_100 = "temp_100"  # signed, raw / 100 = deg C (monitor temperatures)
    TEMP_OFFSET = "temp_offset"  # (raw - 5000) / 100 = deg C
    TEMP_10 = "temp_10"  # raw / 10 = deg C (setpoint and limits)
    CURRENT_100 = "current_100"  # raw / 100 = A
    FREQ = "freq"  # Hz
    RPM = "rpm"
    BOOL = "bool"  # "0" / "1"
    OP_MODE = "op_mode"
    FAN_SPEED = "fan_speed"
    VANE_VERTICAL = "vane_vertical"  # 1..4
    VANE_HORIZONTAL = "vane_horizontal"  # 1..5
    TEXT = "text"  # version strings and the like


@dataclass(frozen=True, slots=True)
class ObjectDef:
    name: str
    order: int | None  # position in a request (related objects together); None: last
    group: Group
    codec: Codec = Codec.RAW
    monitor: bool = False  # part of the 5-minute service monitor
    writable: bool = False  # on the write allow-list (set_level "02")
    sub_id: int = 0  # 0 = indoor unit, 1 = the adapter itself
    long_string: bool = False  # uses much of a reply; read alone


def _rows(
    group: Group, rows: list[tuple[int, str]], codec: Codec = Codec.RAW, *, monitor: bool = False
) -> list[ObjectDef]:
    return [ObjectDef(name, order, group, codec, monitor=monitor) for order, name in rows]


_C, _S, _G = Group.CAPABILITY, Group.STATE, Group.SETTINGS

# fmt: off
_CAPABILITY = _rows(_C, [
    (1, "iu_has_mtim"), (2, "iu_af_inc_vrt"), (3, "iu_af_inc_hrz"),
    (4, "iu_has_af_wide"), (5, "iu_af_fan_pattern"), (6, "iu_hmn_det"),
    (7, "iu_auto_save_time"), (8, "iu_auto_off_time"), (9, "iu_dry_type"),
    (10, "iu_has_comf_af"), (11, "iu_has_ai_eco"), (12, "iu_has_heat_ster"),
    (13, "iu_has_ai_timer"), (14, "iu_side_fan_fnc"), (15, "iu_ai_auto_fnc"),
    (16, "iu_consign"), (17, "iu_has_srv_mntr"), (18, "iu_has_fld_set"),
    (19, "iu_has_auto_clean"), (20, "iu_ai_fnc"), (21, "iu_tmp_inc"),
    (22, "iu_has_hmd_snsr"), (23, "iu_has_watch"), (24, "iu_has_upd_main"),
    (25, "iu_has_upd_eep"), (26, "iu_has_tmp_lmt"),
]) + _rows(_C, [
    (27, "iu_tmp_lo_auto"), (28, "iu_tmp_hi_auto"), (29, "iu_tmp_lo_heat"),
    (30, "iu_tmp_hi_heat"), (31, "iu_tmp_lo_cool"), (32, "iu_tmp_hi_cool"),
], Codec.TEMP_10)

_STATE = [
    ObjectDef("iu_set_tmp", 35, _S, Codec.TEMP_10, writable=True),
    ObjectDef("iu_fan_spd", 36, _S, Codec.FAN_SPEED, writable=True),
    ObjectDef("iu_af_dir_vrt", 41, _S, Codec.VANE_VERTICAL, writable=True),
    ObjectDef("iu_af_swg_vrt", 42, _S, Codec.BOOL, writable=True),
    ObjectDef("iu_af_dir_hrz", 43, _S, Codec.VANE_HORIZONTAL, writable=True),
    ObjectDef("iu_af_swg_hrz", 44, _S, Codec.BOOL, writable=True),
    ObjectDef("iu_malfnc_stat", 47, _S, Codec.INT),
    ObjectDef("iu_err_code", 48, _S, Codec.RAW),
    ObjectDef("iu_indoor_tmp", 49, _S, Codec.TEMP_OFFSET),  # never written
    ObjectDef("iu_wifi_led", 51, _S, Codec.BOOL, writable=True),
    *_rows(_S, [
    (37, "iu_set_tmp_1"), (38, "iu_int_op_mode"), (39, "iu_dry_adj"),
    (40, "iu_watch_stat"), (45, "iu_af_dir_cntr_vrt"), (46, "iu_af_dir_cntr_hrz"),
    (50, "iu_hmd"), (52, "iu_af_dir_side_vrt"), (53, "iu_af_dir_side_hrz"),
    (54, "iu_fan_side_spd"),
    ]),
]

_SETTINGS = [
    ObjectDef("iu_economy", 55, _G, Codec.BOOL, writable=True),
    ObjectDef("iu_min_heat", 56, _G, Codec.BOOL, writable=True),
    ObjectDef("iu_fan_ctrl", 59, _G, Codec.BOOL, writable=True),
    ObjectDef("iu_powerful", 70, _G, Codec.BOOL, writable=True),
    # Works on single splits; a multi-split outdoor unit refuses it (write_res
    # "unknown") unless DIP switch SET1-4 is on. Refusals are handled at runtime.
    ObjectDef("ou_low_noise", 71, _G, Codec.BOOL, writable=True),
    # Human sensor for energy saving (KGTG, KJCAL): nobody in the room for about
    # 20 minutes moves the setpoint (heat -4 C, cool +2 C at most). 0/1 as read.
    ObjectDef("iu_hmn_det_auto_save", 57, _G, Codec.BOOL, writable=True),
    ObjectDef("iu_hmn_det_auto_off", 58, _G, Codec.INT),
    ObjectDef("iu_demand", 60, _G, Codec.INT),  # read-only demand response status
    *_rows(_G, [
    (61, "iu_push_ntfy"), (62, "iu_clean_sug"), (63, "iu_malfnc_disp"),
    (64, "iu_malfnc_disp_3d"), (65, "iu_dry_cool_jp"), (66, "iu_ai_eco_jp"),
    (67, "iu_auto_clean_jp"), (68, "iu_ai_ecosw_jp"), (69, "iu_side_fan"),
    (72, "iu_anti_frz"), (73, "iu_fltr_sign_reset"), (74, "iu_coil_dry"),
    (75, "iu_mtim1_set"), (76, "iu_mtim2_set"), (77, "iu_mtim3_set"),
    (78, "iu_mtim4_set"), (79, "iu_mtim1_h"), (80, "iu_mtim1_m"),
    (81, "iu_mtim2_h"), (82, "iu_mtim2_m"), (83, "iu_mtim3_h"),
    (84, "iu_mtim3_m"), (85, "iu_mtim4_h"), (86, "iu_mtim4_m"),
    (87, "iu_mtim_stat"), (88, "iu_mtim1_valid"), (89, "iu_mtim2_valid"),
    (90, "iu_mtim3_valid"), (91, "iu_mtim4_valid"), (92, "iu_ai_time_h"),
    (93, "iu_ai_time_m"), (94, "iu_ai_timer_stat"), (95, "iu_ai_timer"),
    (96, "iu_comf_af"), (97, "iu_ai_timer_fnc"),
    ]),
]

_M = Group.INDOOR_MONITOR
_INDOOR_MONITOR = [
    ObjectDef("iu_suct_tmp", 98, _M, Codec.TEMP_100, monitor=True),
    ObjectDef("iu_indoor_tmp_crct", 99, _M, Codec.TEMP_100, monitor=True),
    ObjectDef("iu_heat_ex_tmp", 100, _M, Codec.TEMP_100, monitor=True),
    ObjectDef("iu_fan_rot1", 101, _M, Codec.RPM, monitor=True),
    ObjectDef("iu_fan_rot2", 102, _M, Codec.RPM, monitor=True),
    ObjectDef("iu_fan_rot3", 103, _M, Codec.RPM, monitor=True),
    ObjectDef("iu_drain_pump", 104, _M, Codec.RAW, monitor=True),
    ObjectDef("iu_eng_time", 105, _M, Codec.INT, monitor=True),
    ObjectDef("iu_fltr_time", 106, _M, Codec.INT, monitor=True),
    ObjectDef("iu_fan1_time", 107, _M, Codec.INT, monitor=True),
    ObjectDef("iu_fan2_time", 108, _M, Codec.INT, monitor=True),
    ObjectDef("iu_fan3_time", 109, _M, Codec.INT, monitor=True),
    ObjectDef("iu_frz_prev", 110, _M, Codec.INT, monitor=True),
]

_AI = _rows(Group.AI, [
    (111, "iu_ai_sug_time"), (112, "iu_mnt_icon"), (113, "iu_ai_eco"),
    (114, "iu_heat_ster"), (115, "iu_ai_infr_chg"), (116, "iu_on_timer_sug"),
])

_POWER_MISC = _rows(Group.POWER_MISC, [
    (117, "iu_pow_cons"), (118, "iu_malfnc_detail"), (119, "iu_room"),
    (121, "iu_buzzer"), (123, "hmdf_onoff"), (124, "hmdf_stat"),
])

_O = Group.OUTDOOR_MONITOR
_OUTDOOR_MONITOR = [
    ObjectDef("iu_outdoor_tmp", 125, _O, Codec.TEMP_OFFSET),
    ObjectDef("ou_outdoor_tmp", 126, _O, Codec.TEMP_100, monitor=True),
    ObjectDef("ou_disc_tmp", 127, _O, Codec.TEMP_100, monitor=True),
    ObjectDef("ou_heat_ex_tmp", 128, _O, Codec.TEMP_100, monitor=True),
    ObjectDef("ou_heat_ex_tmp_out", 129, _O, Codec.TEMP_100, monitor=True),
    ObjectDef("ou_fan_rot1", 130, _O, Codec.RPM, monitor=True),
    ObjectDef("ou_fan_rot2", 131, _O, Codec.RPM, monitor=True),
    ObjectDef("ou_cmp_freq", 132, _O, Codec.FREQ, monitor=True),
    ObjectDef("ou_exp_vlv1", 133, _O, Codec.INT, monitor=True),
    ObjectDef("ou_exp_vlv2", 134, _O, Codec.INT, monitor=True),
    ObjectDef("ou_exp_vlv3", 135, _O, Codec.INT, monitor=True),
    ObjectDef("ou_exp_vlv4", 136, _O, Codec.INT, monitor=True),
    ObjectDef("ou_exp_vlv5", 137, _O, Codec.INT, monitor=True),
    ObjectDef("ou_four_way_vlv", 138, _O, Codec.INT, monitor=True),
    ObjectDef("ou_current", 139, _O, Codec.CURRENT_100, monitor=True),
    ObjectDef("iu_cmp_time", 140, _O, Codec.INT, monitor=True),
    ObjectDef("ou_fan1_time", 141, _O, Codec.INT, monitor=True),
    ObjectDef("ou_fan2_time", 142, _O, Codec.INT, monitor=True),
]

_VERSIONS = [
    ObjectDef("iu_main_ver", 144, Group.VERSIONS, Codec.TEXT, long_string=True),
    ObjectDef("iu_eep_ver", 145, Group.VERSIONS, Codec.TEXT, long_string=True),
]

_OTHER = _rows(Group.OTHER, [(33, "wla_has_sremo"), (34, "wla_has_wtim")])

_SPECIAL = [
    ObjectDef("iu_onoff", 120, Group.SPECIAL, Codec.BOOL, writable=True),
    ObjectDef("iu_op_mode", 122, Group.SPECIAL, Codec.OP_MODE, writable=True),
    ObjectDef("ou_model", None, Group.SPECIAL, Codec.TEXT, long_string=True),
]

_A = Group.ADAPTER
_ADAPTER = [
    ObjectDef("wla_main_ver", None, _A, Codec.TEXT, sub_id=1, long_string=True),
    ObjectDef("wla_model", None, _A, Codec.TEXT, sub_id=1),
    ObjectDef("wla_serial", None, _A, Codec.TEXT, sub_id=1),
    ObjectDef("wla_local_ip_addr", None, _A, Codec.TEXT, sub_id=1),
    ObjectDef("wla_rssi", None, _A, Codec.INT, sub_id=1),
    ObjectDef("wla_cloud_mode", None, _A, Codec.INT, sub_id=1),
    ObjectDef("wla_ota_status", None, _A, Codec.TEXT, sub_id=1),
    ObjectDef("wla_led", None, _A, Codec.INT, sub_id=1),
    ObjectDef("wla_log_level", None, _A, Codec.INT, sub_id=1),
    ObjectDef("wla_cnct_ssid", None, _A, Codec.TEXT, sub_id=1),
    ObjectDef("wla_wlan_ch", None, _A, Codec.INT, sub_id=1),
    ObjectDef("wla_mem_use", None, _A, Codec.INT, sub_id=1),
    ObjectDef("wla_op_time", None, _A, Codec.RAW, sub_id=1),
    ObjectDef("wla_ai_ver", None, _A, Codec.TEXT, sub_id=1),
    # Unknown to firmware L45-2 (measured), kept so a probe can confirm it.
    ObjectDef("wla_dev_sub_id", None, _A, Codec.RAW, sub_id=1),
    ObjectDef("wla_info", None, _A, Codec.RAW, sub_id=1),
]
# fmt: on

INDOOR: tuple[ObjectDef, ...] = tuple(
    _CAPABILITY + _STATE + _SETTINGS + _INDOOR_MONITOR + _AI + _POWER_MISC
    + _OUTDOOR_MONITOR + _VERSIONS + _OTHER + _SPECIAL
)  # fmt: skip
ADAPTER: tuple[ObjectDef, ...] = tuple(_ADAPTER)

BY_NAME: dict[tuple[int, str], ObjectDef] = {(o.sub_id, o.name): o for o in INDOOR + ADAPTER}

# Named, but never read or written by this package: iu_fld_set_start enters
# function-setting mode and iu_test_op is a test-run control.
NEVER_TOUCH = frozenset({"iu_fld_set_start", "iu_test_op"})

# Names that adapter firmware L45-2 does not know (error 0013 when read alone).
KNOWN_UNKNOWN_L45 = frozenset({
    "iu_has_af_wide", "iu_af_fan_pattern", "iu_has_watch", "iu_af_dir_cntr_vrt",
    "iu_af_dir_cntr_hrz", "iu_af_dir_side_vrt", "iu_af_dir_side_hrz", "iu_fan_side_spd",
    "iu_dry_cool_jp",
})  # fmt: skip

WRITABLE = frozenset(o.name for o in INDOOR if o.writable)
MONITOR = tuple(o.name for o in INDOOR if o.monitor)

ANCHOR_INDOOR = "iu_set_tmp"
ANCHOR_ADAPTER = "wla_rssi"
MAX_NAMES = 20


def get(name: str, sub_id: int = 0) -> ObjectDef | None:
    return BY_NAME.get((sub_id, name))


def function_setting_name(number: int) -> str:
    """Function setting NN is object 143 + NN, reached as iu_fld_setNN (read only)."""
    if not 0 <= number <= 99:
        raise ValueError("function settings are numbered 00..99")
    return f"iu_fld_set{number:02d}"
