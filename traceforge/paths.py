"""Resolve immutable resources separately from writable application state."""
from __future__ import annotations

import os
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = MODULE_ROOT.parent
# Wheel resources take precedence. A neighboring directory must never replace
# the installed demo or the benchmark oracle just because its name matches.
RESOURCE_ROOT = MODULE_ROOT / "_assets" if (MODULE_ROOT / "_assets").is_dir() else PACKAGE_ROOT
DEMO_ROOT = RESOURCE_ROOT / "examples" / "checkout-service"
CASES_ROOT = RESOURCE_ROOT / "benchmarks" / "cases"
WEB_ROOT = RESOURCE_ROOT / "web" / "dist"


def default_data_dir() -> Path:
    """Keep checkout behavior; installed wheels write into the user's data area."""
    if RESOURCE_ROOT == PACKAGE_ROOT and (PACKAGE_ROOT / "pyproject.toml").is_file():
        return PACKAGE_ROOT / ".traceforge"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "TraceForge"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TraceForge"
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "traceforge"
