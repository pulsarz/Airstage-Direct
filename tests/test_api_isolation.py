"""The tools load the API without running the integration or importing Home Assistant."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECK = """
import sys
from tools.airstage.apiload import load_api
api = load_api()
assert api.transport.Transport is not None
assert "custom_components.airstage_direct" not in sys.modules, "integration __init__ ran"
assert not [m for m in sys.modules if m.split(".")[0] == "homeassistant"]
import tools.airstage.dump, tools.airstage.bench, tools.airstage.watch
assert "custom_components.airstage_direct" not in sys.modules
assert not [m for m in sys.modules if m.split(".")[0] == "homeassistant"]
print("ok")
"""


def test_tools_load_the_api_in_isolation() -> None:
    out = subprocess.run(
        [sys.executable, "-c", CHECK], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
