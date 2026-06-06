"""Brand resolution shared across render / feeds / filter / plan / app.

The whole app runs in one process (FastAPI + a ThreadPoolExecutor), so a module
global is enough to track which brand is active without rewriting config.yaml on
every switch (which would lose the file's comments).
"""
from __future__ import annotations
from pathlib import Path
from typing import Any

DEFAULT_THEME = {
    "navy": "#0A0F1E", "navy2": "#0C1426",
    "accent": "#00B4C8", "accent2": "#00C896", "text": "#FFFFFF",
}

# Active-brand override set by the UI switcher. Persisted to a small state file so
# it survives a server restart / uvicorn --reload (otherwise it would silently
# revert to the config default and quietly start using the wrong brand).
_ACTIVE_OVERRIDE: str | None = None
_STATE_FILE = Path(".active_brand")


def set_active(key: str | None) -> None:
    global _ACTIVE_OVERRIDE
    _ACTIVE_OVERRIDE = key or None
    try:
        if _ACTIVE_OVERRIDE:
            _STATE_FILE.write_text(_ACTIVE_OVERRIDE, encoding="utf-8")
        elif _STATE_FILE.exists():
            _STATE_FILE.unlink()
    except OSError:
        pass


def _persisted_key() -> str | None:
    try:
        if _STATE_FILE.exists():
            return _STATE_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


def get_active_override() -> str | None:
    return _ACTIVE_OVERRIDE or _persisted_key()


def list_brands(config: dict) -> dict[str, str]:
    """{key: display name} for the brand switcher."""
    return {k: (v.get("name", k)) for k, v in config.get("brands", {}).items()}


def active_key(config: dict) -> str | None:
    brands = config.get("brands", {})
    override = _ACTIVE_OVERRIDE or _persisted_key()
    if override and override in brands:
        return override
    key = config.get("active_brand")
    if key in brands:
        return key
    return next(iter(brands), None)


def resolve_brand(config: dict, key: str | None = None) -> dict[str, Any]:
    """Return the active (or named) brand dict with theme defaults filled in.

    Falls back to a legacy top-level ``brand:`` block if no ``brands`` map exists.
    """
    brands = config.get("brands", {})
    if key and key in brands:
        b = dict(brands[key])
    else:
        ak = active_key(config)
        b = dict(brands.get(ak, {})) if ak else dict(config.get("brand", {}))
    b["theme"] = {**DEFAULT_THEME, **(b.get("theme") or {})}
    return b


def theme_css(brand: dict) -> str:
    """Inline CSS overriding the brand.css :root variables for this brand."""
    t = {**DEFAULT_THEME, **(brand.get("theme") or {})}
    return (
        ":root{"
        f"--navy:{t['navy']};--navy-2:{t['navy2']};"
        f"--teal:{t['accent']};--green:{t['accent2']};"
        f"--white:{t.get('text', '#FFFFFF')};"
        "}"
    )


def brand_template(brand: dict, kind: str, default: str) -> str:
    """Template filename for a slide kind (title/content/outro/cover) for this
    brand, falling back to the shared default if the brand defines no override."""
    return (brand.get("templates") or {}).get(kind, default)


def brand_feeds_config(config: dict, brand: dict) -> dict:
    """A shallow config copy whose ``feeds`` is this brand's feeds, so the
    existing feeds.py helpers (which read ``config['feeds']``) work unchanged."""
    scoped = dict(config)
    scoped["feeds"] = brand.get("feeds", config.get("feeds", {}))
    return scoped
