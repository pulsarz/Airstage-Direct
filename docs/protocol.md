# The adapter's local API

How Fujitsu Airstage Wi-Fi adapters (tested: AP-WH3E-B, adapter firmware
E120V02P01L45-2) answer on the local network, as this integration uses it.
Most of it was checked on real units, with details and dates in
[measurements.md](measurements.md); points that were not checked say so. Not affiliated
with Fujitsu, and nothing here is an official description.

## Requests

Every call is an HTTP `POST` of a JSON body to `http://<adapter>/<endpoint>`
(port 80). These are the endpoints used here:

| Endpoint | What it does |
| --- | --- |
| `GetParam` | Read named values |
| `SetParam` | Write named values |
| `SetTime` | Set the adapter's clock |
| `Register` / `Unregister` | Ask the adapter to push changes to a listener |
| `SetPollingList` | Choose what is pushed (see [Push](#push)); only the measurement tools send it |

The adapter has other endpoints (timers, firmware updates); this integration
never calls them.

**One request at a time.** Send the next request only after the previous
answer. One request per second ran for hours without a problem; a benchmark
found the adapter handles at most about 0.8 requests per second however fast
they are sent, and faster sending only makes answers slower.

**Latency** is 0.05–2 s and comes in steps of about 350 ms; 20 names take
about 1 s. A 3 s timeout leaves room.

### Common fields

```json
{
  "device_id": "AABBCCDDEEFF",
  "device_sub_id": 0,
  "req_id": "",
  "modified_by": "",
  "set_level": "03",
  "list": ["iu_onoff", "iu_set_tmp"]
}
```

- **`device_id` is the adapter's Wi-Fi MAC address**, as 12 uppercase hex
  digits without separators. An empty, wrong or lowercase one is rejected
  with error `0002`. It can be found in the router's ARP table, and every push
  carries it (see below). Answers echo the `device_id` sent.
- **`device_sub_id`** `0` is the indoor unit, `1` the adapter itself (the
  `wla_*` names).
- `req_id` and `modified_by` may be empty; they are echoed back.
- **`set_level`** is `"03"` for reads and `"02"` for normal writes. Other
  levels exist; level `"05"` writes to function settings put the unit into
  its function-setting mode, which **switches the unit off**. Never use
  anything but `"03"` and `"02"`.

### Errors

The answer has `"result": "OK"` or `"NG"` with an `"error"` code:

| Code | Meaning |
| --- | --- |
| `0002` | A required field is missing or invalid (seen for an empty, lowercase or wrong `device_id`) |
| `0013` | The name is not known to this adapter |

`result` only says whether the request was well formed; whether the indoor
unit took a write is in `write_res` (see [SetParam](#setparam)).

## GetParam

```json
{"device_id": "AABBCCDDEEFF", "device_sub_id": 0, "req_id": "", "modified_by": "",
 "set_level": "03", "list": ["iu_onoff", "iu_op_mode", "iu_set_tmp"]}
```

Answer:

```json
{"device_id": "AABBCCDDEEFF", "device_sub_id": 0, "req_id": "", "modified_by": "",
 "set_level": "03", "read_res": "ack", "result": "OK", "error": "",
 "value": {"iu_onoff": "1", "iu_op_mode": "4", "iu_set_tmp": "220"}}
```

- At most **20 names** per request (larger requests were not tried).
- **Indoor values are strings**; adapter values (sub-id 1) are typed JSON
  (`"wla_rssi": -52`).
- `"65535"` means the unit does not support the object, or, just after the
  unit is switched on, that it has no value yet. `""` means no value right
  now (for example outdoor values while the compressor is off).
- **An answer can be cut short.** Some batches come back with their last few
  names missing, still with `result` OK. The same names in a different order
  can come back complete. Long strings (`ou_model`, versions) make it worse.
  So a missing name proves nothing: check every requested name against the
  answer and ask again for what is missing, on its own if needed. A name that
  is truly unknown gives `0013` when read alone.
- On sub-id 1, an unknown name read alone gives `result` OK with an empty
  `value` instead of `0013`.

### Discovery

There is no announcement on the network. The integration finds adapters by
sending a `GetParam` with an empty `device_id` to every address in the
subnet: an adapter answers with `{"result": "NG", "error": "0002"}`, which no
other device does. The MAC then comes from the ARP table.

## SetParam

```json
{"device_id": "AABBCCDDEEFF", "device_sub_id": 0, "req_id": "", "modified_by": "",
 "set_level": "02", "value": {"iu_set_tmp": "225"}}
```

Answer:

```json
{"device_id": "AABBCCDDEEFF", "device_sub_id": 0, "req_id": "", "modified_by": "",
 "set_level": "02", "write_res": "ack", "read_res": "ack", "cause": "",
 "result": "OK", "error": "", "value": {"iu_set_tmp": "225"}}
```

- Send all six keys. Values are **strings**.
- `write_res` is the indoor unit's verdict: `ack` accepted; refused writes
  answered `unknown`. Other values (`nack`, `timeout`) were not seen and are
  treated as failures.
- `value` echoes the object's value after the write, so the write can be
  checked in the same call.
- Several names in one request are accepted, but `write_res` is a single
  answer for all of them. The integration sends one name per request, in the
  order power, mode, setpoint, fan, then the rest.
- Some writes are refused by the indoor unit (`write_res` `unknown`): the room
  temperature `iu_indoor_tmp` and `iu_demand` at every write level tried;
  `ou_low_noise` on a multi-split outdoor unit (it is reported to work on
  single splits). The integration
  never writes the first two and treats a refused `ou_low_noise` as "not
  available on this unit".
- **A switched-off unit refuses setpoint changes** (reported by the owner,
  2026-11-16). Switch it on first, or keep the change until it is on.

## SetTime

```json
{"device_id": "AABBCCDDEEFF", "device_sub_id": 0, "req_id": "", "modified_by": "",
 "unix_time": 1763230440}
```

`unix_time` is an **integer** (Unix seconds). The answer is the usual
`result` / `error`.

After a power cycle the adapter has no clock, and its service monitor (below)
stays on placeholders: intake and coil at exactly 20.00 °C, outdoor values
empty. Setting the time brings the monitor back within a few minutes. The
integration sends `SetTime` only when it sees these placeholders for several
reads in a row, because setting the clock moves the monitor's refresh moment
and `wla_op_time`.

## The service monitor

The detailed values (coil and intake temperatures, fan rpm, compressor
frequency, current, outdoor temperatures, valves, run-time counters) are not
live. Each adapter refreshes them **every 300 s on its own clock**, at its own
offset (one test adapter about 100 s past each 5-minute mark of Unix time,
another right on the mark). Reads in between return the same values, so
reading more often gains nothing.

At the refresh the indoor values update and **every outdoor value reads
`""` for 20–50 s**; the run-time counters come back last. A read about 60 s
after the refresh gets everything. The integration learns each adapter's
offset by polling a few names until they change, then reads once per refresh
at offset + 60 s.

## Push

Register a listener:

```json
POST /Register
{"Type": "1", "Key": "02AABBCCDDEE", "IP": "192.0.2.10"}
```

- No `device_id` in this body.
- `Key` identifies the registration: a MAC-like string. Registering again
  with the same `Key` is expected to reuse its registration (not tested).
  `Unregister` with `{"Key": "..."}` removes it.
- `IP` is the listener's own address. The adapter then POSTs to
  `http://<IP>:10275/NtfyParam`; **the port is fixed**.
- An adapter keeps only a few registrations (reportedly 5, a new one replacing
  the oldest; not tested), and they are likely lost when the adapter
  restarts. The phone app may hold some of them. The integration renews its
  registration when pushes stop.

A push body:

```json
{"value": {"iu_set_tmp": "225"}, "device_id": "AABBCCDDEEFF", "device_sub_id": 0}
```

- **Every push carries the adapter's MAC** as `device_id`.
- **State changes are pushed** 1–8 s after they happen, including changes
  made with the IR remote: power, mode, setpoint, fan (louvres only if
  listed, see below). A push carries only the changed objects.
- **A periodic report every 300 s**, with a few objects such as `wla_rssi`,
  `iu_indoor_tmp`, sometimes `iu_outdoor_tmp`. Which objects differs per
  adapter.
- **Service-monitor values are never pushed**, on change or in the periodic
  report, whatever the polling list says. They must be read with `GetParam`.
- In pushes, adapter values arrive on sub-id 0 and as strings.
- An empty HTTP `200` is a sufficient reply.
- An object `iu_monitor1` (sub-id 1, 33 comma-separated numbers) is sometimes
  pushed; its meaning is not known, and the integration ignores it.

`SetPollingList` chooses what is pushed:

```json
{"device_id": "AABBCCDDEEFF",
 "init_ntfy": ["iu_onoff", "iu_op_mode", "iu_set_tmp", "iu_fan_spd"],
 "change_monitor": ["iu_onoff", "iu_op_mode", "iu_set_tmp", "iu_fan_spd"],
 "cycle_monitor": ["wla_rssi", "iu_indoor_tmp"]}
```

- `init_ntfy` is sent once, about 12 s after the list is set; `change_monitor`
  is pushed on change; `cycle_monitor` goes into the periodic report, but
  monitor objects listed there are silently dropped.
- **There appears to be one list per adapter**, and the Fujitsu cloud or app
  set it too: an adapter we never sent a list to had its own, different from
  another's. So a list you set may be replaced, and setting one changes what
  the cloud gets.
- The integration never sends `SetPollingList`. It relies on the adapter's
  existing list for state pushes, also reads the state every minute, and
  reads everything else with `GetParam`. With push switched off in its
  settings it reads the state every 10 s.

## Values

| Object | Raw | Meaning |
| --- | --- | --- |
| `iu_onoff` | `"1"` / `"0"` | on / off |
| `iu_op_mode` | `0`–`4` | 0 auto, 1 cool, 2 dry, 3 fan, 4 heat |
| `iu_set_tmp` | `"220"` | ÷ 10 °C (22.0) |
| `iu_fan_spd` | `0`, `2`, `5`, `7`, `8`, `9`, `11` | auto, quiet, low, low-medium, medium, medium-high, high |
| `iu_af_dir_vrt` | `1`–`4` | vertical louvre, 1 most horizontal, 4 straight down |
| `iu_af_dir_hrz` | `1`–`5` | horizontal louvre, 1 far left, 3 middle, 5 far right |
| `iu_af_swg_vrt`, `iu_af_swg_hrz` | `0` / `1` | swing off / on |
| `iu_indoor_tmp`, `iu_outdoor_tmp` | `"7350"` | (raw − 5000) ÷ 100 °C (23.5) |
| Monitor temperatures (`iu_suct_tmp`, `iu_heat_ex_tmp`, `ou_*_tmp`) | `"3050"` | ÷ 100 °C, can be negative |
| `iu_tmp_lo_heat` and the other limits | `"160"` | ÷ 10 °C |
| `ou_current` | `"350"` | ÷ 100 A |
| `ou_cmp_freq` | `"46"` | Hz |
| `iu_fan_rot1`, `ou_fan_rot1` | `"960"` | rpm |
| `wla_rssi` | `-52` | dBm |
| `wla_op_time` | | follows the adapter's clock; not an uptime counter |

Fan, louvre and swing codes were checked by setting each value in the app and
reading it back; heat (4) and on/off were read on running units. The other
mode codes have not been checked one by one, and nothing has been checked in
cooling yet.
