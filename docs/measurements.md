# Measurements on the real units

Measurements on the test system, newest first: one multi-split outdoor unit
(AOEH30KBTA4) with ASEH14KJCAL ("Downstairs") and ASEH07KJCAL ("Upstairs")
indoor units, AP-WH3E-B adapters. [protocol.md](protocol.md) summarises what
they mean. Raw output is not published; MAC, serial, IP addresses and network
name are left out.

## 2026-11-15, night: sensors in Home Assistant

A test build ran on the owner's Home Assistant 2026.11.1. Its debug log
showed one unit's scheduled monitor reads exactly 5 minutes apart (20:17:48,
20:22:48, 20:27:48). It also showed that a unit set up while off got no
room-temperature entity (65535 at the first read, checked only once); since
then the integration checks such objects again after the unit is switched
on.

## 2026-11-15, evening: the integration in Home Assistant

A test build, copied by hand into the owner's Home Assistant 2026.11.1
(Docker, host networking; HACS cannot install from a private repository).
Both units added through the config flow: "Search the network" found the
adapters, the owner only picked an address and entered a name. The search
showed Home Assistant's "Loading next step" screen with the raw entry ID while
it ran (about 15 s for the whole subnet).

- Setpoint, fan, mode and both swing controls worked on both units from Home
  Assistant.
- Changes made with the IR remote appeared in Home Assistant within about
  2 s (by eye, not timed), through push.

## 2026-11-15, 16:08–16:38: push and monitor scheduling on both units

Tool: `watch --register` for 30 minutes on Downstairs and Upstairs, running
`api.push` (one listener, one push session per unit, the tools' key) and
`api.monitor` (one scheduler per unit). No SetPollingList, no state polling,
nothing else talking to the adapters. Both units on, heat.

- **Offsets learned** in the first cycle: Downstairs 101.0 s (measured
  earlier: 95–105 s), Upstairs 1.2 s (refresh at about 298–300 s, seen by the
  next poll). Learning took 20 and 10 polls of 5 names (10 s apart).
- **Exactly one scheduled read per unit per 5 minutes** after learning: six
  each, Upstairs at phase 65.2–65.6 s and Downstairs at 164.9–166.0 s, i.e.
  64–65 s after each refresh. In all twelve reads every outdoor value and
  run-time counter was filled; no missed window, no re-learning.
- Each scheduled read took **3 requests**: the 31 names in two batches, plus
  one name re-requested after being cut short from a batch.
- **Pushes:** the owner changed the Downstairs fan with the app (Medium, then
  Auto); both changes arrived as pushes (16:14:15, 16:14:53), the first
  before the owner's message saying it was done. The periodic report came
  every 300 s from both adapters (Upstairs about 31 s, Downstairs about 44 s
  past the mark), so no re-registration was needed.
- The compressor read 50 Hz on Upstairs at 16:21:05 and 0 Hz on Downstairs at
  16:22:45; the owner confirmed it had stopped in between. Readings from two
  adapters of one outdoor unit agree only when taken in the same cycle.
- Both registrations were removed at the end (`Unregister` OK).

## Fan rpm against rated airflow (KJCAL, heating)

Rated airflow per fan setting from the KJCAL series service manual (the
manual itself is not in the repo), next to the rpm measured on 2026-11-15
(`iu_fan_rot1`; 07KJ under load, 14KJ High to Medium-high under load, the rest
at low load):

| Setting | 07KJ rpm | 07KJ m³/h | 14KJ rpm | 14KJ m³/h |
| --- | --- | --- | --- | --- |
| High | 960 | 540 | 1290 | 720 |
| Medium-high | 890 | 480 | 1120 | 600 |
| Medium | 810 | 430 | 960 | 490 |
| Low-medium | 730 | 380 | 820 | 400 |
| Low | 670 | 330 | 700 | 320 |
| Quiet | 570 | 260 | 610 | 250 |

A straight line fits both models within 1.6 % at every setting:

- ASEH07KJCAL: airflow ≈ 0.706 × rpm − 141 m³/h
- ASEH14KJCAL: airflow ≈ 0.683 × rpm − 163 m³/h

So the measured rpm and the rated airflow agree, the 14KJ's low-load readings
were not held back, and the heat estimate can take airflow straight from the
live `iu_fan_rot1` with a per-model line (or the six points). The manual's
cooling airflow differs only at Low and Quiet (14KJ: 340 and 290 m³/h); the
cooling rpm is not measured yet.

## 2026-11-15, 15:48–15:49: first write, Downstairs

Approved by the owner. A one-off script (not committed) used `api.device` with
a writable transport, one request at a time, 1 s gap. Unit on, heat, setpoint
22.0 C, fan Quiet; nobody touched it meanwhile.

| Step | Sent | Answer | Time |
| --- | --- | --- | --- |
| Write | `{"iu_set_tmp": "225"}` at `"02"` | `write_res` ack, `read_res` ack, `value` `{"iu_set_tmp": "225"}` | 1.30 s |
| Read back 5 s later | GetParam | `225` | |
| Restore (only because it still read `225`) | `{"iu_set_tmp": "220"}` | ack, echo `220` | 0.69 s |
| Two names, both unchanged | `{"iu_set_tmp": "220", "iu_fan_spd": "2"}` | `write_res` **one** `"ack"`, `value` echoes both | 1.00 s |

- The echoed value equalled the read-back, so checking a write against its
  echo works.
- **Several names per SetParam are accepted**, but `write_res` is one string
  for the whole request, so a refusal cannot be traced to one name, and this
  test (unchanged values) does not show that two changes apply together. The
  integration keeps one name per request.
- The unit ended as it started: on, heat, 22.0 C, fan Quiet.

## 2026-11-15, 13:54–14:31: Upstairs (ASEH07KJCAL)

The owner switched the unit on just before 13:54. Tools: `dump`, a
side-by-side read of both adapters, `watch --register --state-interval 5`
for 35 minutes (fan curve), then single reads.

**Same as Downstairs.** Adapter firmware E120V02P01L45-2, model AP-WH3E-B;
the same 9 unknown names, the same 3 names lost in a batch, the same 65535
set except one (below). The MAC needs the same uppercase form.

- **`wla_serial` and the MAC differ** between the two adapters; the MACs share
  the vendor prefix. One more data point that the MAC is a usable unique ID.
- **`iu_indoor_tmp` read `65535` for the first minutes after power-on** (the
  dump at 13:54 and a read at 13:55), then normal values when read alone or
  in a batch (`7875`, `7950` at 14:30–14:31). So `65535` there means "no
  value yet", not "not supported". The periodic push carried a value
  (`7400`) at 13:55:18 already.
- **Monitor refresh offset about 298 s**, i.e. right on each 5-minute mark
  (Downstairs: about 100 s). Each adapter has its own offset, as expected.
- **The periodic report has six objects here**: `wla_rssi`,
  `iu_pow_inf_yest`, `iu_indoor_tmp`, `iu_outdoor_tmp`, `iu_hmd` (`65535`),
  `iu_pow_cons`. This adapter's polling list was never changed by us, so this
  is the list the cloud or the app set. Downstairs' report had only the first
  three, also before we changed its list. So the objects that can go in the
  report differ per adapter. Monitor objects were still never in it.
- A report arrived 3 s after `Register` (13:55:18); whether that was the
  normal 5-minute report or a reaction to `Register` is unclear.
- Same-moment read of both adapters (13:55): `iu_outdoor_tmp` matched
  (`6000`); Upstairs' outdoor block was in its blanking window, and the
  Downstairs compressor read 0 Hz. So matching outdoor values for grouping
  must wait until both blocks are filled and the compressor runs.

**Indoor fan rpm per setting**, ASEH07KJCAL, under load (setpoint 27–30 °C,
coil 43.5–53 °C); the owner went from High down to Quiet, one setting per
refresh. The first High reading (560 rpm at 14:00) came while the coil was
still warming up (34.5 °C) and is left out.

| Setting (code) | rpm | Coil |
| --- | --- | --- |
| High (11) | 960 | 51.50 °C |
| Medium-high (9) | 890 | 50.50 °C |
| Medium (8) | 810 | 46.50 °C |
| Low-medium (7) | 730 | 43.50 °C |
| Low (5) | 670 | 53.00 °C |
| Quiet (2) | 570 | 52.50 °C |

Lower than the 14KJ at every setting (High 960 against 1290), as expected
for the smaller unit.

## 2026-11-15, 13:03–13:47: indoor fan rpm per setting, Downstairs (ASEH14KJCAL)

The owner set each fan speed from the app and held it through one monitor
refresh (the refresh lands about 105 s past each 5-minute mark), while
`watch` polled state every 5 s and the monitor every 15 s. Heat mode.
`iu_fan_rot1` is in rpm.

| Time | Setting (code) | Setpoint | rpm | Coil | Intake | Compressor |
| --- | --- | --- | --- | --- | --- | --- |
| 13:01 | Auto (0) | 22.0 °C | 960 | 44.50 °C | 23.75 °C | 46 Hz |
| 13:06 | Quiet (2) | 22.0 °C | 610 | 34.50 °C | 24.50 °C | 24 Hz |
| 13:11 | Low (5) | 22.0 °C | 700 | 36.50 °C | 24.75 °C | 23 Hz |
| 13:16 | Low-medium (7) | 22.0 °C | 820 | 39.50 °C | 25.00 °C | 22 Hz |
| 13:21 | Medium (8) | 22.0 °C | 960 | 41.50 °C | 24.25 °C | 25 Hz |
| 13:26 | Medium-high (9) | 22.0 °C | 810 | 38.00 °C | 23.75 °C | 19 Hz |
| 13:31 | Medium-high (9) | 22.5 °C | 820 | 37.00 °C | 23.75 °C | 21 Hz |
| 13:36 | High (11) | 25.5 °C | 1290 | 49.50 °C | 25.00 °C | 45 Hz |
| 13:41 | Medium-high (9) | 27.0 °C | 1120 | 47.50 °C | 27.75 °C | 30 Hz |
| 13:46 | Medium (8) | 27.0 °C | 960 | 50.00 °C | 28.50 °C | 30 Hz |

- **At low load the unit holds the fan back.** With the room near the
  setpoint (compressor 19–25 Hz, coil 37–38 °C), Medium-high ran at
  810–820 rpm, below Medium; with real demand (setpoint 27 °C, coil 47.5 °C)
  it ran at 1120 rpm. This fits a warm-air control that limits the fan while
  the coil is not hot. So rpm per setting is not fixed, and the integration
  must use the measured `iu_fan_rot1`, not the setting.
- Under load: Medium 960, Medium-high 1120, High 1290 rpm. Medium read 960
  at both loads. Quiet 610, Low 700 and Low-medium 820 were only measured at
  low load and may be held back too (they rise with the coil temperature).
- Auto ran at the Medium speed (960 rpm) at 13:01.
- For the heat estimate, these rpm values pair with the datasheet airflow per
  setting to give points on an rpm → airflow curve. Still to do: the
  ASEH07KJCAL, and Quiet/Low under load.

## 2026-11-15, 12:48–12:58: fan and louvre codes, Downstairs

The owner stepped through every fan speed and louvre position in the
Airstage app while `watch --register --state-interval 5` polled the state
objects every 5 s. Unit running in heat.

**`iu_fan_spd`**, in the order set, with the app's labels:

| Code | App label |
| --- | --- |
| 0 | Auto |
| 2 | Quiet |
| 5 | Low |
| 7 | Low-medium |
| 8 | Medium |
| 9 | Medium-high |
| 11 | High |

The codes rise with the speed. The app offers seven settings.

**Louvres.** Set from the app's airflow screen (a side view for vertical, a
view from the front for horizontal); a screenshot at vertical 4 and
horizontal 2 shows which number is which:

| Object | Values | Meaning |
| --- | --- | --- |
| `iu_af_dir_vrt` | 1–4 | 1 = most horizontal, 4 = straight down; matches `iu_af_inc_vrt` = 4 |
| `iu_af_dir_hrz` | 1–5 | 1 = far left, 3 = middle, 5 = far right, as the app draws it; `iu_af_inc_hrz` reads 21, meaning unknown |
| `iu_af_swg_vrt`, `iu_af_swg_hrz` | 0 / 1 | swing off / on |

After vertical swing was switched off, the vertical louvre read 4 a few
seconds later; whether swing-off returns it to 4 or the owner set it back is
not known.

**Pushes** with the polling list from the cycle tests (`change_monitor` =
`iu_onoff`, `iu_op_mode`, `iu_set_tmp`, `iu_fan_spd`):

- Every fan and setpoint change was pushed, carrying only the changed
  object, about 1–8 s after the change. Usually the 5 s poll saw it first.
  So "within a second" does not hold; push latency is a few seconds.
- Louvre changes were not pushed (not in `change_monitor`).

## 2026-11-15, 11:42–12:25: benchmark, Downstairs

Tool: `bench`, one request at a time. Names: the 20-name monitor batch
(`iu_set_tmp`, `iu_onoff`, the indoor monitor, the first outdoor monitor
objects); sizes 1, 5 and 10 use its first names. Unit running in heat.

**Latency at a 1 s gap**, 200 requests per size, no errors, no timeouts, no
answer cut short:

| Names | Median | p95 | Min | Max |
| --- | --- | --- | --- | --- |
| 1 | 279 ms | 1093 ms | 53 ms | 1407 ms |
| 5 | 648 ms | 1048 ms | 329 ms | 1408 ms |
| 10 | 689 ms | 1051 ms | 330 ms | 1713 ms |
| 20 | 1047 ms | 1354 ms | 688 ms | 1760 ms |

- **Latency comes in steps of about 350 ms**: answers cluster at roughly 0,
  350, 700, 1050, 1400 and 1750 ms. The adapter seems to fetch values from
  the indoor unit in cycles of about 350 ms, and a request takes a whole
  number of them. 1 name: 0–1 cycles; 5 and 10 names: mostly 1–2; 20 names:
  mostly 3 (125 of 200 at 1.0–1.1 s). So 10 names cost about what 5 do.
- No difference inside and outside the monitor refresh window (phase
  85–160 s): same medians.

**Rate sweep**, 20 names, 100 requests per gap, no errors, no timeouts, no
answer cut short at any gap:

| Gap | Requests/s | Median | p95 | Max |
| --- | --- | --- | --- | --- |
| 1.0 s | 0.50 | 1047 ms | 1403 ms | 1763 ms |
| 0.5 s | 0.65 | 883 ms | 1562 ms | 1599 ms |
| 0.25 s | 0.81 | 789 ms | 1796 ms | 1839 ms |
| 0.1 s | 0.82 | 926 ms | 1948 ms | 1999 ms |

Throughput levels off at about 0.8 requests/s (about 16 names/s) from a
0.25 s gap; going faster only lengthens the latency tail. The adapter
answered normally afterwards. Gaps under 0.1 s and parallel requests were
not tried.

**What this sets**, for the integration: a request timeout of 3 s (slowest
seen 2.0 s); in steady state the rate does not matter (one monitor read per
5 minutes); for one-off work such as the setup probe, a 0.25 s gap is safe
on this adapter. To repeat on an ASEH07KJCAL unit.

## 2026-11-15, 10:35–10:59: `cycle_monitor` test, Downstairs

Two 12-minute `watch --register --polling-list` runs (lists in
`tools/airstage/polling_lists/`), monitor poll every 15 s, unit running in
heat. Both lists: `init_ntfy` and `change_monitor` = `iu_onoff`,
`iu_op_mode`, `iu_set_tmp`, `iu_fan_spd`; `cycle_monitor` = the periodic trio
plus 13 monitor objects. Run 1 had the trio first, run 2 last.

- `SetPollingList` answered `result OK` both times.
- About 12 s after `SetPollingList`, an **initial dump** of the `init_ntfy`
  objects plus `iu_indoor_tmp` arrived. (After `Register` alone, none came.)
- **Every periodic report (4 in total) held only the trio**, in either list
  order. No push at the monitor refreshes either, although all 16 polled
  monitor objects changed. So monitor objects cannot be put in the report.
- Report times: 10:40:04, 10:45:04 (run 1), 10:50:14, 10:55:15 (run 2);
  earlier 10:00:00 and 10:05:00. So the report is every 300 s but its phase
  moves (0, 4, 14 s past the mark) between runs. Its anchor is not known; it
  is not simply 5 minutes after `Register` or `SetPollingList`.
- Monitor refreshes in run 2 at phase 93 s (two cycles), against 100–104 s
  in runs 1 and the first push run: the offset is about 95–105 s on this
  adapter, read with 10–15 s polls.
- **The adapter kept the list from run 2.** A list cannot be read back, so
  the earlier one could not be restored. Whether the Fujitsu cloud or app sets
  its own list again later was not checked.

## 2026-11-15, 09:59–10:06: push registration, Downstairs

Tool: `watch --register` (fixed key, listener on the
tools machine, port 10275), 7 minutes, monitor poll every 15 s. No
`SetPollingList` sent, so the adapter's existing list applied. Unit running
in heat.

- `Register` `{"Type":"1","Key":...,"IP":...}` without `device_id`:
  `result OK`. `Unregister` `{"Key":...}`: `result OK`.
- **No initial dump** followed the registration.
- **Periodic report at exactly :00 and :05** (phase 0–1 s), i.e. on the
  5-minute marks of the clock, not at the monitor refresh (~100 s past the
  mark, seen again at 101.8 s ±15 s). Body:
  `{"value": {"wla_rssi": "-54", "iu_pow_inf_yest": "----/--/--,-,-",
  "iu_indoor_tmp": "7200"}, "device_id": "<MAC>", "device_sub_id": 0}`.
  - **Every push carries the adapter's MAC as `device_id`.** Since `Register`
    needs no MAC, a client can learn it from the first push (within 5 min).
  - `wla_rssi` is a string in the push but a number in `GetParam`.
  - `iu_pow_inf_yest` holds no data (`----/--/--,-,-`).
- **Unrequested `iu_monitor1` pushes** at 10:04:28 and 10:04:47 (phase 268
  and 287 s), with `device_sub_id` **1**: a string of 33 comma-separated
  numbers. First `0,0,0,0,0,0,0,0,1,0,…` (index 8 = 1), then all zeros. So
  the existing polling list contains `iu_monitor1`, the bundle is not always
  all zeros, and its pushes did not line up with the monitor refresh. What
  index 8 means is open (nobody touched the unit, as far as known).
- The listener's reply was an empty `200`; the adapter did not retry.

## 2026-11-15, 09:13–09:33: Downstairs running (heat)

The owner turned the unit on just before 09:13. Tools: `watch` for 13 min
(monitor poll every 10 s, no push registration), a second full `dump` at
phase 160–199 s, and single test reads. Phases below are unix time mod 300 s,
measured with a 10 s poll, so each is good to about ±10 s.

### Monitor refresh schedule

Three refreshes seen (09:16:4x, 09:21:3x, 09:26:3x). Each one:

| Phase | What changes |
| --- | --- |
| ~90–100 s | Indoor values update (`iu_suct_tmp`, `iu_indoor_tmp_crct`, `iu_heat_ex_tmp`, `iu_fan_rot1`) **and every outdoor value turns `""`** |
| ~105–115 s | Outdoor block back: temperatures, `ou_cmp_freq`, `ou_current`, `ou_fan_rot1`, `ou_exp_vlv1`, `ou_four_way_vlv` |
| ~115–125 s | `ou_exp_vlv5` back |
| ~125–145 s | `iu_cmp_time`, `ou_fan1_time` back (in the third cycle not yet back 25 s after the refresh) |

So this adapter's refresh offset is about **95 s past each 5-minute mark**,
and for 20–50 s after it the outdoor values read `""`. This is a blanking
window: at 10 s resolution, compressor
frequency and current arrived together with the other outdoor values. A read
at offset + 35 s would miss the run-time counters; offset + 60 s got
everything. The first refresh after start-up was different: no blanking, and
`ou_cmp_freq` updated together with the indoor values.

### Batch answers are cut short

Reproducible, independent of the refresh window: some batches come back with
their **last** names missing. The same names in another order come back
complete.

| Batch (sub-id 0) | Names | Returned |
| --- | --- | --- |
| 5 malfunction and humidifier objects (`iu_malfnc_detail` … `hmdf_stat`), `iu_outdoor_tmp`, 12 × outdoor monitor, anchor first | 20 | 18: `ou_exp_vlv5`, `ou_four_way_vlv` (positions 18–19) lost, three runs out of three |
| the same, reversed | 20 | 20 |
| the same, those 5 moved to the end | 20 | 19: last one lost |
| the same without those 5 | 15 | 15 |
| anchor, those 5, `ou_exp_vlv5`, `ou_four_way_vlv` | 8 | 8 |
| `ou_current` … `iu_op_mode` with `ou_model` last | 12 | 11: `ou_model` lost |
| the same with `ou_model` second | 12 | 5: everything after the 5th name lost, including `iu_onoff` and `iu_op_mode` |
| monitor-only batches of 20 and 13 (`watch`) | 20, 13 | complete in 78 of 78 polls |

The answer is still `result OK`, `read_res ack`. A guess that fits: the
adapter packs the indoor unit's reply into a fixed number of fixed-size
frames, and strings or specially handled objects (`ou_model`) waste room. Not
proven. **Consequence:** check every requested name against the answer and
re-request what is missing; group batches by object range; read `ou_model`
on its own.

### Values while running

| Object | Raw values seen | Reading |
| --- | --- | --- |
| `ou_cmp_freq` | `52`, `25`, `34` | Hz |
| `ou_current` | `570`, `240`, `350` | ÷100 A |
| `ou_heat_ex_tmp_out` | `-1400`, `100` | ÷100 °C, **can be negative** (−14.00 °C right after start) |
| `ou_disc_tmp` | `4100`, `3600`, `3900` | ÷100 °C (41.00 °C). The `5300` in the first dump was with the compressor at 0 Hz |
| `ou_four_way_vlv` | `1` in heat | 1 = heating position (to confirm in cool) |
| `ou_exp_vlv1`, `ou_exp_vlv5` | `130`, `74`, `68` (both equal every time) | valve steps; vlv5 tracks vlv1 here |
| `ou_fan_rot1` | `850`, `550`, `700` | rpm |
| `iu_fan_rot1` | `0` → `820` → `960` | rpm; `iu_fan_spd` read `8` (it read `0` at 09:02; whether someone changed it is not recorded) |
| `iu_pow_cons` | `8299` → `8304` in 26 min | rises while running; maybe cumulative 0.1 kWh (0.5 kWh in 26 min ≈ 1.1 kW). Unconfirmed |
| `ou_model` | `""` while running too | gives no model on this unit |
| `wla_op_time` | `3987155060` → `1211497624` 26 min later | **not an uptime counter** (went down); unusable for restart detection |

## 2026-11-15: first dump, Downstairs (ASEH14KJCAL)

Tool: `python -m tools.airstage dump`, one request at a time, 1 s gap, 11
requests in 17 s. Adapter `wla_model` AP-WH3E-B, `wla_main_ver`
E120V02P01L45-2; indoor `iu_main_ver` E101V07P00L02-1, `iu_eep_ver`
E000V01P00L01-1. Read at the time: `iu_onoff` 1 (on), `iu_op_mode` 4 (heat), setpoint
22.0 °C, fan `0` (auto), compressor at 0 Hz.

### Findings

- **An empty `device_id` is rejected** with `{"result":"NG","error":"0002"}`
  on this firmware (L45-2), for every request. The MAC is required, written
  as **12 uppercase hex digits without separators**; the same MAC in lowercase also gets `0002`. Answers
  echo the `device_id` sent.
- **`iu_onoff` reads `"1"`** while on (`"0"` when off), so the unit was on at 09:02 (idle at
  0 Hz) and was switched off some time between 09:05 and 09:13, when the
  owner turned it back on. An off reading is still to be seen.
- **Names missing from an answer are not always unknown.** The first dump
  had 16 names missing from its batch answers. Read one at a time:
  - 9 are unknown to this firmware (error `0013` when read alone):
    `iu_has_af_wide`, `iu_af_fan_pattern`, `iu_has_watch`,
    `iu_af_dir_cntr_vrt`, `iu_af_dir_cntr_hrz`, `iu_af_dir_side_vrt`,
    `iu_af_dir_side_hrz`, `iu_fan_side_spd`, `iu_dry_cool_jp`.
  - 3 are known but were left out of the batch answer: `ou_four_way_vlv`,
    `ou_exp_vlv5`, `ou_model`. Alone they answered (`1`, `480`, `""` at
    09:05; all three `""` at 09:11). Two sat at the end of a 20-name batch,
    one at the end of a 12-name batch. Why the adapter drops them is not known
    yet; it is not answer size (complete answers were larger). Until it is,
    **an absent name proves nothing**: reread it alone. `dump` now does this.
  - 4 adapter names on sub-id 1 are unknown: `wla_dev_sub_id`,
    `wla_has_sremo`, `wla_has_wtim`, `wla_info`. On sub-id 1 an unknown
    name alone gives `result OK` with an empty `value`, not `0013`.
    `wla_has_sremo` and `wla_has_wtim` on sub-id 0 answer `65535`.
- **Adapter values are typed JSON**, not strings: `wla_rssi` -52,
  `wla_cloud_mode` 1, `wla_led` 1, `wla_log_level` 3, `wla_wlan_ch` 6,
  `wla_mem_use` 1860160, `wla_op_time` 3987155060 (unit not known yet).
  Indoor values are always strings.
- **`iu_fltr_sign_reset` answers `65535`** on this unit, and so does
  `iu_has_tmp_lmt`.

### Encodings this dump settles or suggests

| Object | Raw | Reading | Status |
| --- | --- | --- | --- |
| Monitor temperatures (`iu_suct_tmp`, `iu_heat_ex_tmp`, `iu_indoor_tmp_crct`, `ou_outdoor_tmp`, `ou_heat_ex_tmp`) | `2600`, `3050`, `2300`, `300`, `400` | ÷100 °C: 26.00, 30.50, 23.00, 3.00, 4.00 | ÷100 confirmed; ÷10 would give 260 °C |
| `iu_indoor_tmp` (GetParam, not only push) | `7350` | (raw − 5000) ÷ 100 = 23.5 °C | same encoding as in a push |
| `iu_outdoor_tmp` | `5300` | (raw − 5000) ÷ 100 = 3.0 °C | matches `ou_outdoor_tmp` 3.00 °C |
| `ou_disc_tmp` | `5300` | 53.00 °C if ÷100 | unclear; needs a reading with the compressor running |
| `iu_set_tmp` | `220` | ÷10 = 22.0 °C | confirmed |
| `iu_tmp_lo/hi_heat`, `_cool`, `_auto` | `160`/`300`, `180`/`300`, `180`/`300` | ÷10 °C: heat 16–30, cool 18–30, auto 18–30 | |
| `ou_current` | `30` | 0.30 A (÷100), compressor at 0 Hz | |
| `iu_fan_rot1`, `ou_fan_rot1` | `550`, `60` | rpm? indoor fan at 550 on auto; outdoor 60 unclear | to check against a fan sweep |
| `iu_pow_cons` | `8299` | unknown unit | |
| `iu_af_inc_vrt`, `iu_af_inc_hrz` | `4`, `21` | probably louvre step counts / capability codes | to check with a louvre sweep |

### Off-state and idle values

At 09:02 (unit on, compressor at 0 Hz), most outdoor objects still had
values (`ou_outdoor_tmp`, `ou_disc_tmp`, `ou_heat_ex_tmp`,
`ou_heat_ex_tmp_out` 0, `ou_fan_rot1`, `ou_cmp_freq` 0, `ou_exp_vlv1`,
`ou_current`). Empty (`""`): `ou_fan_rot2`, `ou_exp_vlv2`–`4`, `iu_cmp_time`,
`ou_fan1_time`, `ou_fan2_time`, the second and third indoor fan, the drain
pump, and the AI, humidifier and room objects.

### Latency (from the dump, not the benchmark)

| Request | Elapsed |
| --- | --- |
| 1 indoor name | 0.074–0.081 s |
| 20 indoor names | 0.69–1.26 s (monitor and outdoor batches slowest) |
| 12 indoor names | 0.74 s |
| 18 adapter names (sub-id 1) | 0.075 s |

Indoor-unit objects cost roughly 35–60 ms each, which suggests the adapter
asks the indoor unit per object rather than answering from a cache. A 20-name
read can take well over 1 s, so the 5 s timeout leaves room but a 1 s one
would not. `bench` gives the real distribution.
