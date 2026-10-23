"""Resolve the operator's 41 checkout paths independently of site-packages."""
from __future__ import annotations

import os
from pathlib import Path


_MODULE_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_WORKING_PACKAGE_ROOT = Path.cwd()
if not os.environ.get("AOITALK_PLUGIN_ROOT") and (_WORKING_PACKAGE_ROOT / "aoitalk_plugin").is_dir():
    _MODULE_PACKAGE_ROOT = _WORKING_PACKAGE_ROOT
elif not os.environ.get("AOITALK_PLUGIN_ROOT"):
    _checkout_package_root = _WORKING_PACKAGE_ROOT / "plugins" / "AoiTalk_Plugin"
    if (_checkout_package_root / "aoitalk_plugin").is_dir():
        _MODULE_PACKAGE_ROOT = _checkout_package_root

PACKAGE_ROOT = Path(os.environ.get("AOITALK_PLUGIN_ROOT", str(_MODULE_PACKAGE_ROOT))).expanduser().resolve()
REPOSITORY_ROOT = Path(
    os.environ.get("AOITALK_REPOSITORY_ROOT", str(PACKAGE_ROOT.parents[1]))
).expanduser().resolve()
