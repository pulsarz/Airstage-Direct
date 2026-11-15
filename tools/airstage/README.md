# Measurement tools

Tools for the Airstage adapters. They run on the integration's API
package (`custom_components/airstage_direct/api/`), loaded by file path, so they
never import the integration itself or Home Assistant. They need the dev venv
(Python 3.14, `uv sync`; current Home Assistant needs it). Run from the repo root:

```bash
uv run python -m tools.airstage dump
```

Output goes to `tools/airstage/out/` (ignored). Units come from
`tools/airstage/units.local.json` (ignored) or `--unit NAME=IP[,MAC]`:

```json
[{"name": "Downstairs", "host": "192.0.2.10", "model": "ASEH14KJCAL"}]
```

The adapter needs its MAC as `device_id`. The tools try the one given (or an
empty one), then take the MAC from the ARP table; pass `,MAC` if that fails.

| Command | What it does |
| --- | --- |
| `dump [--functions]` | Read every named object (and, opt-in, function settings 00–99). Reports `unknown_names` and names `lost_in_batch` (cut short, then recovered) |
| `bench [--count 200] [--sizes 1,5,10,20]` | GetParam latency per batch size. `--sweep 0.5,0.25 --allow-faster` tries faster rates and stops at the first error |
| `watch [--duration S] [--interval 10] [--state-interval 5]` | Push listener on :10275 plus a log of monitor (and state) changes. `--register` registers for push and unregisters on exit; `--polling-list @file` also sends SetPollingList; `--stop-file PATH` ends it cleanly |
| `python -m tools.airstage.make_fixtures` | Rebuild the redacted `tests/fixtures/` from `out/` |

Safety comes from `api/transport.py`: the tools create read-only transports, so
SetParam can never be sent; only GetParam (set_level 03), Register,
Unregister and, on request, SetPollingList go out; one request in flight per
adapter; at least 1 s between requests unless `bench --allow-faster`.
`iu_fld_set_start` and `iu_test_op` are never read.

Checks: `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`.
