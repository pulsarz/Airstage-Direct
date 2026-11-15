# Estimates: how they are calculated

Heat output, energy, COP, SCOP and SEER are **calculated, not measured**. Every
one says so: its name starts with "Estimated" (metered electricity excepted)
and it carries `estimate: true`, `method` and `accuracy` attributes. Expect
**±20–25 %** on any single value; hourly and longer averages, trends and
month-to-month comparisons are what they are good for. The unit's service
values behind them refresh only every 5 minutes, so anything shorter than an
hour means little.

## Heat output of one indoor unit

    Q = V × 1.2 kg/m³ × 1005 J/(kg·K) × ε × (T_coil − T_intake)

- **V, the airflow**, from the unit's measured fan speed through a straight
  line per model: the fan speed per setting, measured on the units, against
  the rated airflow per setting from the KJCAL service manual (within 1.6 %).

  | Model | Airflow (m³/h) |
  | --- | --- |
  | ASEH07KJCAL | 0.706 × rpm − 141 |
  | ASEH14KJCAL | 0.683 × rpm − 163 |

  The adapter does not report its indoor unit's model, so pick it in the
  unit's settings. Other models can use "custom" with their own line; a unit
  without a model gets no heat estimate.
- **ε, coil effectiveness**: how far the air gets from the intake to the coil
  temperature. A system setting gives its value at the **High** fan airflow
  (default 0.85, a typical value, not measured). Slower air spends longer in
  the coil, so with the model's rated High airflow known, ε follows the
  airflow like a coil at constant refrigerant temperature:

  ε = 1 − exp(−NTU), NTU = NTU_High × (V / V_High)^−0.4, NTU_High = −ln(1 − ε_High)

  The exponent comes from the air side carrying most of the heat-transfer
  resistance (heat transfer ∝ airflow^0.6, typical for finned coils). With
  0.85 at High this gives about 0.87 at 80 % of the High airflow, 0.90 at
  60 % (around Medium) and 0.93 at 40 % (around Quiet); an exponent of 0.3 or
  0.5 moves Quiet by about ±0.015. The built-in models know their rated High
  airflow (07KJ 540 m³/h, 14KJ 720 m³/h); a "custom" model can add it, and
  without it ε stays fixed. This assumes every coil reaches the same ε at its
  own rated airflow. The sensors show the ε used (`coil_effectiveness`) and
  the setting (`coil_effectiveness_at_high`).
- An **airflow scale** (default 1.0) is a system setting.
- **T_intake** is the raw intake sensor. Coil heat warms it a little, so the
  estimate leans low.
- **Zero** when the unit is off, only runs its fan, or the fan stands still.
  Heating is never negative (defrost has not been measured); cooling is never
  positive. In Auto the sign of T_coil − T_intake decides.
- **Cooling** is sensible only ("(sensible only)" in the name) unless the unit
  has a room humidity sensor: then the moisture removed is added, assuming the
  air leaves the coil at most saturated at the coil temperature. Not yet
  checked on a unit in cooling.

## System figures (the outdoor unit's device)

- **Heat / cooling output, total**: the sum of the units. A unit that is on
  but has no fresh values makes the total unknown rather than too low.
- **Heat / cooling energy**: the totals over time, in kWh. Held between
  readings; a gap of more than 10 minutes adds nothing. Kept across restarts.
- **Electricity, heating / cooling**: the airco's own kWh meter, if chosen in
  the system settings, split by season (any unit heating: heating; all off:
  the last season). Without a meter: **Estimated electricity** from the
  outdoor unit's current × 230 V (indoor fans not included).
  Every meter reading is counted at once, but these sensors are updated at
  most once a minute and after each unit update, so a fast meter does not
  fill the recorder.
- **COP (1 h)**: heat (or cooling) energy over electricity in the last hour,
  standby included. Shown from 15 minutes of data on; until the window covers
  55 minutes it is provisional (`provisional: true`, `window_minutes`). The
  window is kept across restarts. Unknown while less than 0.05 kWh was used.
- **SCOP / SEER (this month, this year)**: the same over the calendar month
  and year (heating for SCOP, cooling for SEER).
- **Carnot maximum COP**: 0.5 × T_hot / (T_hot − T_cold) in kelvin, from the
  warmest indoor coil and the coldest outdoor coil (the other way round when
  cooling). A plausibility bound: an estimated COP above it points at a wrong
  setting.

Nothing here writes to the units.
