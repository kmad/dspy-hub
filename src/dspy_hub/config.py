"""Configuration helpers for dspy-hub."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


REGISTRY_ENV_VAR = "DSPY_HUB_REGISTRY"
CONFIG_ENV_VAR = "DSPY_HUB_CONFIG"


@dataclass(slots=True)
class Settings:
    """Runtime configuration for the CLI."""

    registry: str


def _default_config_path() -> Path:
    if custom := os.environ.get(CONFIG_ENV_VAR):
        return Path(custom).expanduser()

    if os.name == "nt":  # pragma: no cover - platform specific path
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "dspy-hub" / "config.json"


def _default_registry_path() -> str:
    from importlib import resources

    try:
        registry_root = resources.files("dspy_hub").joinpath("sample_registry")
        # resources.as_file ensures compatibility with zipped installs.
        with resources.as_file(registry_root) as extracted:
            return str(extracted / "index.json")
    except FileNotFoundError:  # pragma: no cover - should not happen locally
        return str(Path(__file__).resolve().parent / "sample_registry" / "index.json")


def load_settings() -> Settings:
    """Load configuration, honouring the environment, config file, and defaults."""

    if registry := os.environ.get(REGISTRY_ENV_VAR):
        return Settings(registry=registry)

    config_path = _default_config_path()
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text())
            registry_from_file = data.get("registry")
            if registry_from_file:
                return Settings(registry=registry_from_file)
        except json.JSONDecodeError:
            # Fall back to default registry when config is invalid.
            pass

    return Settings(registry=_default_registry_path())


def ensure_config_dir_exists() -> Optional[Path]:
    """Ensure the configuration directory exists, returning the directory path."""

    config_path = _default_config_path()
    config_dir = config_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir