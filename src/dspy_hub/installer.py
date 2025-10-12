"""Package installation helpers for dspy-hub."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from .exceptions import InstallationError, RegistryError
from .repository import Package, PackageRepository


@dataclass(slots=True)
class InstallOptions:
    destination: Path
    force: bool = False
    dry_run: bool = False


class Installer:
    """Install packages from a repository onto the local filesystem."""

    def __init__(self, repository: PackageRepository):
        self.repository = repository

    def install(self, package: Package, options: InstallOptions) -> List[Path]:
        destination_root = options.destination.expanduser().resolve()
        installed_paths: List[Path] = []

        for file_spec in package.files:
            if not isinstance(file_spec, dict):
                raise InstallationError(
                    f"Invalid file entry for package '{package.name}': expected an object"
                )

            source = file_spec.get("source") or file_spec.get("path")
            if not source:
                raise InstallationError(
                    f"File entry for package '{package.name}' is missing a 'source' attribute"
                )

            target_relative = file_spec.get("target") or Path(source).name
            target_path = (destination_root / target_relative).resolve()

            try:
                target_path.relative_to(destination_root)
            except ValueError as exc:
                raise InstallationError(
                    f"Refusing to install outside the destination root: {target_relative}"
                ) from exc

            if target_path.exists() and not options.force:
                raise InstallationError(
                    f"Target file already exists: {target_path}. Use --force to overwrite."
                )

            if options.dry_run:
                installed_paths.append(target_path)
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = self.repository.fetch_bytes(source)
            except RegistryError as exc:
                raise InstallationError(str(exc)) from exc

            target_path.write_bytes(data)
            installed_paths.append(target_path)

        return installed_paths