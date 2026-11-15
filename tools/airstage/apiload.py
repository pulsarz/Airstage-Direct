"""Load the integration's API package without running the integration itself.

custom_components/airstage_direct/__init__.py imports Home Assistant, so
importing the API the normal way would need Home Assistant installed. This
loads custom_components/airstage_direct/api/ by file path under its real
module name, so the tools and the tests share one copy of it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

API_NAME = "custom_components.airstage_direct.api"
API_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "airstage_direct" / "api"


def load_api() -> ModuleType:
    if (loaded := sys.modules.get(API_NAME)) is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(
        API_NAME, API_DIR / "__init__.py", submodule_search_locations=[str(API_DIR)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load {API_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[API_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[API_NAME]
        raise
    return module
