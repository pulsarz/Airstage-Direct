# Airstage Direct

A Home Assistant custom integration (HACS) that controls Fujitsu Airstage
air conditioners directly over the local network through their built-in
Wi-Fi adapters. No cloud. Domain: `airstage_direct`. Display name:
"Airstage Direct for Fujitsu". Not affiliated with Fujitsu.

Read `docs/protocol.md` and `docs/measurements.md` before changing anything
that talks to an adapter.

## Sources

- Protocol knowledge comes from `docs/` and from measurements on real units.
  Do not search for, read or reuse code from other projects that talk to these
  adapters.
- When the docs and a measurement disagree, the measurement wins: update
  `docs/measurements.md` with what was observed and the date, and
  `docs/protocol.md` if the summary changes.
- Never commit Fujitsu firmware, app files, manuals or large extracts from them.
  Object names, codes and behaviour described in our own words are fine.

## Real units

Details of the test system, if any, are in `CLAUDE.local.md`, which is not
committed. Safety rules, always:

- One request in flight per adapter, never parallel. At most about one request
  per second unless a benchmark has shown more is safe.
- Reads only, unless the task is explicitly about a write. Control writes use
  `set_level` "02".
- Never send `set_level` "04" or "05" (function-setting mode switches the unit
  off), never write `iu_indoor_tmp`, never write unnamed objects, never call
  firmware-update or other endpoints not listed in `docs/protocol.md`.
- Do not change the units' mode, setpoint or power during measurements unless
  asked; people live with them.
- Unregister any push registration a tool created when it exits; adapters only
  keep a few.
- Do not run the tools against an adapter while Home Assistant is using it
  unless the owner agrees.

## Layout

```
custom_components/airstage_direct/      the integration
custom_components/airstage_direct/api/  pure-Python API layer, no HA imports
tools/airstage/                         measurement CLI: dump, bench, watch
tests/                                  pytest, simulator adapter, fixtures
tests/fixtures/                         recorded adapter answers (redacted JSON)
docs/                                   protocol, measurements, estimates
```

Nothing is published to PyPI; `manifest.json` has no external requirements.

## Code standards

- Python version and HA APIs as required by the current Home Assistant release.
  Fully async, never block the event loop. `aiohttp` for HTTP.
- Typed throughout; `mypy --strict` and `ruff` (lint and format) must pass.
- The `api/` package must not import `homeassistant`.
- Entities: `has_entity_name`, translated names (`strings.json`, `en`),
  diagnostic entities disabled by default, nothing requested for disabled
  entities.
- Anything calculated rather than measured (heat output, COP, SCOP, SEER) is
  named "Estimated ..." and carries `estimate: true`, `method` and `accuracy`
  attributes.
- No per-unit settings hardcoded: the user picks the model and options.
- Tests with `pytest`, `pytest-asyncio` and
  `pytest-homeassistant-custom-component`, against the simulator in `tests/`.
  New behaviour comes with tests. Record real adapter answers as fixtures
  instead of inventing them, but redact them first: MAC / `device_id`,
  `wla_serial`, IP addresses and `wla_cnct_ssid` replaced by placeholders.
  Raw tool output stays in the ignored `tools/airstage/out/`.
- Checks: `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`,
  `uv run mypy`.

## Git

- Small commits with clear messages in English; work on branches, not `main`.
- Never put a claude.ai session link or a `Claude-Session` line in commits,
  pull requests, issues or anywhere else on GitHub.
- License: Apache-2.0.
