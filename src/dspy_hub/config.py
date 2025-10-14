"""Configuration helpers for dspy-hub."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_REGISTRY_URL = "https://api.dspyhub.com/index.json"


@dataclass(slots=True)
class Settings:
    """Runtime configuration for the CLI."""

    registry: str = DEFAULT_REGISTRY_URL


def load_settings() -> Settings:
    """Return default runtime settings."""

    return Settings()
