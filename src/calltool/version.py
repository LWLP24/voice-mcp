from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version


def runtime_version() -> str:
    release = os.environ.get("CALLTOOL_VERSION", "").strip().removeprefix("v")
    if release:
        return release
    try:
        return version("calltool")
    except PackageNotFoundError:
        return "0.1.1-dev.local"
