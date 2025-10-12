"""Custom exceptions for the dspy-hub CLI."""


class RegistryError(RuntimeError):
    """Raised when the registry cannot be accessed or parsed."""


class PackageNotFoundError(RegistryError):
    """Raised when a package is not present in the registry."""


class InstallationError(RuntimeError):
    """Raised when an installation fails."""