"""Constants for Airstage Direct for Fujitsu."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "airstage_direct"
MANUFACTURER: Final = "Fujitsu"

SUBENTRY_INDOOR_UNIT: Final = "indoor_unit"

CONF_DEVICE_ID: Final = "device_id"  # the adapter's MAC, 12 uppercase hex digits
CONF_CAPABILITIES: Final = "capabilities"
CONF_PORT: Final = "port"  # not in the UI; 80 in practice, set by tests
CONF_MONITOR_OFFSET: Final = "monitor_offset"  # learned refresh offset, seconds

STATE_POLL_S: Final = 60.0
STATE_POLL_NO_PUSH_S: Final = 10.0  # when the push listener cannot run
RECHECK_UNSUPPORTED_S: Final = 300.0  # a unit just switched on can answer 65535 for a while
RECHECK_AFTER_ON_S: Final = 180.0  # after the unit is switched on
RECHECK_INTERVAL: Final = timedelta(hours=1)  # while anything is still 65535

ISSUE_PUSH_UNAVAILABLE: Final = "push_unavailable"
ISSUE_CLOCK_LOST: Final = "clock_lost"
REQUEST_LOG_SIZE: Final = 100

# Per indoor unit (subentry data), for the estimates.
CONF_MODEL: Final = "model"  # "", one of api.estimates.FAN_CURVES, or MODEL_CUSTOM
CONF_CURVE_SLOPE: Final = "curve_slope"  # m3/h per rpm, for MODEL_CUSTOM
CONF_CURVE_INTERCEPT: Final = "curve_intercept"  # m3/h, for MODEL_CUSTOM
CONF_RATED_AIRFLOW: Final = "rated_airflow"  # m3/h at High, for MODEL_CUSTOM (optional)
CONF_HUMIDITY_ENTITY: Final = "humidity_entity"  # room humidity, for cooling estimates
MODEL_NONE: Final = ""
MODEL_CUSTOM: Final = "custom"

# Per system (entry options).
OPT_ESTIMATES: Final = "estimates"
OPT_EFFECTIVENESS: Final = "coil_effectiveness"
OPT_AIRFLOW_SCALE: Final = "airflow_scale"
OPT_METER: Final = "electricity_meter"  # an energy (kWh/Wh) sensor for the airco alone
OPT_PREFERRED_UNIT: Final = "preferred_outdoor_unit"  # subentry id
OPT_PUSH: Final = "push"
MAINS_VOLTAGE: Final = 230.0  # for the estimated electricity without a meter
ESTIMATE_TICK: Final = timedelta(seconds=60)
# Subentry data that is bookkeeping, not configuration: no reload when it changes.
STORED_ONLY: Final = frozenset({"capabilities", "monitor_offset"})
