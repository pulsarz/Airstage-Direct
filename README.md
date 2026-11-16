<p align="center"><img src="custom_components/airstage_direct/brand/icon.png" width="128" alt=""></p>

# Airstage Direct for Fujitsu

A Home Assistant integration that controls Fujitsu air conditioners with an
Airstage Wi-Fi adapter **directly over your local network**. No cloud
account, no internet connection: Home Assistant talks to the adapters and the
adapters push changes back within seconds.

> **Beta.** Tested on one multi-split system (ASEH14KJCAL and ASEH07KJCAL
> indoor units, AP-WH3E-B adapters), in heating. Cooling has not been tested
> on real units yet, and the estimates are just that. Reports from other
> models are very welcome.
>
> Not affiliated with, endorsed by or supported by Fujitsu. "Fujitsu" and
> "Airstage" are trademarks of their owners.

## What you get

Per indoor unit:

- **Climate:** on/off, mode (auto, cool, dry, fan, heat), setpoint, fan speed
  (auto, quiet, low, low-medium, medium, medium-high, high), vertical and
  horizontal swing, presets (eco, boost, minimum heat) where the unit has them.
- **Louvre positions** (selects) and **switches** for economy, powerful,
  minimum heat, energy-saving fan, outdoor low noise, the Wi-Fi LED and the
  human sensor, where the unit supports them.
- **Sensors:** room, intake and coil temperature, indoor fan speed, error
  code, a malfunction binary sensor; Wi-Fi signal and run-time counters
  (disabled by default).

For the outdoor unit (one device per system, shared by all indoor units):

- Outdoor temperature, compressor frequency, current, a compressor-running
  binary sensor; discharge and coil temperatures, fan speed and valves
  (disabled by default).

Optional **estimates**, clearly labelled "Estimated": heat and cooling output
per unit, heat energy, COP over the last hour, SCOP and SEER per month and
year, and a Carnot upper bound as a sanity check. They use the measured fan
speed and coil temperatures; with your own electricity meter for the airco
they get much better. How they work and how far to trust them (about
±20–25 %): [docs/estimates.md](docs/estimates.md).

Only what an enabled entity needs is read, and the adapters are never asked
more than once per second.

## Requirements

- Home Assistant 2026.10 or newer.
- Fujitsu indoor units with an Airstage Wi-Fi adapter that are already
  connected to your Wi-Fi, for example with the Fujitsu app. The integration
  does not use the cloud. Running with the adapters cut off from the internet
  has not been tested; the state is also read every minute, so at worst
  changes would show up more slowly.
- Home Assistant must be able to receive connections on **TCP port 10275**
  from the adapters (for push). In Docker, use host networking or publish
  that port.

## Installation

**HACS:** add this repository as a custom repository (type: Integration),
install "Airstage Direct for Fujitsu", and restart Home Assistant.

**Manually:** copy `custom_components/airstage_direct` into your
`config/custom_components/` folder and restart Home Assistant.

## Setup

1. Settings → Devices & services → Add integration → **Airstage Direct for
   Fujitsu**. Give the system (one outdoor unit) a name.
2. Add your first indoor unit: **Search the network** finds the adapters on
   your subnet (about 15 seconds), or enter the adapter's IP address. Give it
   a name.
3. Add more indoor units to the same system with **Add indoor unit**.
4. Optional, per indoor unit (its settings): pick the **model** so the heat
   estimate knows its fan curve, and a room humidity sensor for the cooling
   estimate.
5. Optional, system settings: switch estimates off, pick an **electricity
   meter** (kWh) for the airco, the indoor unit to take outdoor values from,
   or switch push off (then the state is polled every 10 s).

Give the adapters a fixed IP address in your router; if one changes, update
it in the indoor unit's settings.

## Experimental: short-cycling

Some units start and stop the compressor every few minutes in mild weather
("short-cycling"), which wastes energy and wears the compressor. These
system settings help to see it; they are off unless you set them, and may
change:

- **Power sensor for the outdoor unit**: a power meter (W or kW) for the airco
  alone. Adds "Compressor starts" and "Compressor starts, last hour" to the
  outdoor unit, with the last run and pause. The on and off levels can be
  adjusted to your meter.
- **Current temperature on the thermostat card**: the room temperature
  (default) or the corrected intake, the value the unit regulates on (the
  tested unit stopped when it reached setpoint + 1 °C). It is updated every
  5 minutes.

- **Short-cycle guard** (off by default; it steers your units, so read this
  first):
  - *Fan while heating:* when a unit heats with its fan on Auto, the guard
    uses a fixed speed instead (Low-medium by default). On a short-cycling unit,
    Auto ran the fan so slowly that the unit measured its own warm air and
    stopped early. Any other fan choice is left alone; Auto comes back when
    the guard is switched off or the unit stops heating.
  - *Delay restarts:* needs the power sensor and a room temperature sensor
    in each indoor unit's settings. After the compressor stops while the room
    is at or above your setpoint, the guard lowers that unit's setpoint a little (0.5 °C by
    default) until the room has cooled 0.3 °C below your setpoint, then puts
    it back. The thermostat card keeps showing your setpoint. A cold room is
    never held.
  - *Fail-safe:* your setpoint is stored before the guard changes it and put
    back on every way out: the room cooled, the room sensor stopped
    reporting, 45 minutes passed, the unit was switched off or out of heat,
    the guard was switched off, or Home Assistant restarted. Any setpoint or
    fan change you make (app, remote or Home Assistant) wins at once. If the
    guard has to write more than 20 times in an hour for one unit, it stops
    for that unit.
  - The thermostat entity shows what it is doing (`guard`, and after each
    delay `guard_last_hold` with how long it held and how the room moved).
  - Switch off any automation of your own that sets the fan, and try it on
    one unit first.

What was measured on a short-cycling unit, and what helped:
[docs/measurements.md](docs/measurements.md).

## Good to know

- **Monitor values update every 5 minutes.** Temperatures other than the room
  temperature, the fan speed and everything from the outdoor unit come from
  the adapter's service monitor, which only refreshes every 5 minutes. The
  integration reads it once per refresh, at the right moment.
- **Changes you make** with the remote, the app or Home Assistant show up
  within a few seconds through push.
- **After a power cut** the adapter loses its clock and its monitor stops.
  The integration notices and sets the clock; only if the values still do not
  come back does it show a repair notice.
- **Outdoor low noise** is ignored by some multi-split outdoor units; the
  switch then shows it was refused.
- The adapter keeps only a few push registrations, shared with the Fujitsu
  app.

## Troubleshooting

**The search finds no adapters.** The search only covers Home Assistant's own
network (subnet). If the adapters are on another subnet or VLAN, or Home
Assistant runs in Docker without host networking, choose "Enter the IP
address" instead. If the setup then asks for the MAC address, the adapter is
not in Home Assistant's ARP table (another subnet): it is printed on the
adapter and shown in your router.

**"No answer from this address".** Check the IP address in your router, and
that the adapter is connected (the Fujitsu app can still reach it). An adapter
answers on port 80 only.

**Changes made with the remote take up to a minute to show.** Push updates are
not arriving, so the integration falls back to reading the state every
minute. The adapters must be able to reach Home Assistant on TCP port 10275:
use host networking in Docker (or publish port 10275), and allow the port in
any firewall between them. If Home Assistant cannot open the port at all, a
repair notice says so, and the state is read every 10 seconds instead.

**Temperatures and outdoor values change only every 5 minutes.** That is how
often the adapter refreshes them; reading more often would not help.

**Values stay unknown or frozen after a power cut.** The adapter lost its
clock. The integration sets it, and the values return within about
5 minutes. If they do not, a repair notice appears: switch the adapter (the
indoor unit's power) off and on again.

**An indoor unit added while it was off has no room temperature.** The unit
reports no value until it runs for a while. The integration checks again
after the unit is switched on and adds the missing entities by itself.

**"Outdoor low noise" switches back off.** Some outdoor units, multi-splits
in particular, refuse it; the switch then shows `refused_by_unit`.

**The adapter's IP address changed.** Open the indoor unit's settings
(Reconfigure) and enter the new address. A fixed address in your router
avoids this.

**Another local client stopped getting updates after you added the
integration.** An adapter keeps only a few push registrations, and Home
Assistant uses one per adapter, so a new one can replace the oldest.

**The estimates are unknown.** Pick the indoor unit's model in its settings;
without one there is no heat estimate. The hourly COP appears after
15 minutes of data (marked provisional until it covers 55 minutes) and needs
at least 0.05 kWh of electricity in that time.

**Reporting a problem.** Open an issue with the integration's diagnostics
(the device page → Download diagnostics). Addresses and MAC addresses are
removed from it, but have a look before you post it.

## Documentation

- [docs/protocol.md](docs/protocol.md): the adapter's local API as this
  integration uses it.
- [docs/measurements.md](docs/measurements.md): what was measured on real
  units, with dates.
- [docs/estimates.md](docs/estimates.md): the estimate formulas.
- [tools/airstage/README.md](tools/airstage/README.md): read-only measurement
  tools for adding support for other models.

## Development

Python 3.14 and [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy
```

The tests run against a simulated adapter built from recorded (redacted)
answers.

## License

Apache-2.0. See [LICENSE](LICENSE).
