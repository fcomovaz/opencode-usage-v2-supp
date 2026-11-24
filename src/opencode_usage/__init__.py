"""OpenCode usage statistics CLI."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def _resolve_version() -> str:
    """Return installed distribution version, with fallback for legacy or editable setups."""
    for dist_name in ("opencode-usage-v2", "opencode-usage"):
        try:
            return version(dist_name)
        except PackageNotFoundError:
            continue
    return "0.0.0+unknown"


__version__ = _resolve_version()
