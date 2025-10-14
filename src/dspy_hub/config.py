"""Configuration helpers for dspy-hub."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DEFAULT_REGISTRY_URL: Final[str] = "https://api.dspyhub.com/index.json"
REGISTRY_ENV_VAR: Final[str] = "DSPY_HUB_REGISTRY"


@dataclass(slots=True)
class Settings:
    """Runtime configuration for the CLI and SDK."""

    registry: str = DEFAULT_REGISTRY_URL


def load_settings() -> Settings:
    """Return runtime settings, honoring environment overrides."""

    override = os.getenv(REGISTRY_ENV_VAR)
    if override is not None:
        candidate = override.strip()
        if candidate:
            if "://" not in candidate:
                candidate = str(Path(candidate).expanduser())
            return Settings(registry=candidate)

    return Settings()
