"""dspy_hub - DSPy Hub CLI and SDK."""

from importlib.metadata import PackageNotFoundError, version

from .hub import (
    HubFile,
    HubPackage,
    load_from_hub,
    load_program_from_hub,
    save_program_to_hub,
    save_to_hub,
)


def __getattr__(name: str):
    if name == "__version__":
        try:
            return version("dspy-hub")
        except PackageNotFoundError:  # pragma: no cover - fallback for dev installs
            return "0.0.0"
    raise AttributeError(f"module 'dspy_hub' has no attribute {name!r}")


__all__ = [
    "__version__",
    "HubFile",
    "HubPackage",
    "load_from_hub",
    "load_program_from_hub",
    "save_to_hub",
    "save_program_to_hub",
]
