"""Registry access for dspy-hub."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from .exceptions import PackageNotFoundError, RegistryError


_SUPPORTED_SCHEMES = {"", "file", "http", "https"}


@dataclass(slots=True)
class Package:
    """A package exposed by the registry."""

    name: str
    version: str
    description: str
    author: str
    files: Sequence[dict]
    raw: dict

    @classmethod
    def from_dict(cls, data: dict) -> "Package":
        required = {"name", "version", "description", "files", "author"}
        missing = [key for key in required if key not in data]
        if missing:
            raise RegistryError(
                f"Package entry is missing required fields: {', '.join(missing)}"
            )
        files = data.get("files")
        if not isinstance(files, list) or not files:
            raise RegistryError(
                f"Package '{data.get('name', '<unknown>')}' must provide a non-empty 'files' list"
            )
        return cls(
            name=str(data["name"]),
            version=str(data["version"]),
            description=str(data["description"]),
            author=str(data["author"]),
            files=files,
            raw=data,
        )

    @property
    def tags(self) -> Sequence[str]:
        tags = self.raw.get("tags", [])
        return tags if isinstance(tags, list) else []

    @property
    def homepage(self) -> str | None:
        homepage = self.raw.get("homepage")
        return str(homepage) if homepage else None

    @property
    def slug(self) -> str:
        return f"{self.author}/{self.name}"


class PackageRepository:
    """Abstraction over a registry of DSPy packages."""

    def __init__(self, index_location: str):
        self.index_location = index_location
        parsed = urlparse(index_location)
        if parsed.scheme not in _SUPPORTED_SCHEMES:
            raise RegistryError(
                f"Unsupported registry scheme '{parsed.scheme}'. Supported schemes: http, https, file"
            )

        if parsed.scheme in ("", "file"):
            index_path = parsed.path if parsed.scheme else index_location
            self._index_path = Path(index_path).expanduser().resolve()
            if not self._index_path.is_file():
                raise RegistryError(f"Registry index not found at {self._index_path}")
            self._base_path = self._index_path.parent
            self._fetcher = self._fetch_local
            self._loader = self._load_local
        else:
            self._index_url = index_location
            self._base_url = index_location.rsplit("/", 1)[0] + "/"
            self._fetcher = self._fetch_remote
            self._loader = self._load_remote

        self._index_cache: dict | None = None
        self._packages_cache: List[Package] | None = None

    # ------------------------------------------------------------------
    # Loading and discovery helpers
    # ------------------------------------------------------------------
    def load_index(self) -> dict:
        if self._index_cache is None:
            self._index_cache = self._loader()
        return self._index_cache

    def list_packages(self) -> List[Package]:
        if self._packages_cache is None:
            index = self.load_index()
            packages_data = index.get("packages")
            if not isinstance(packages_data, list):
                raise RegistryError("The registry index must include a 'packages' list")
            packages = [Package.from_dict(data) for data in packages_data]
            packages.sort(key=lambda pkg: (pkg.author.casefold(), pkg.name.casefold()))
            self._packages_cache = packages
        return list(self._packages_cache)

    def get_package(self, identifier: str) -> Package:
        if not identifier or not identifier.strip():
            raise PackageNotFoundError("Package identifier cannot be empty")

        identifier = identifier.strip()
        packages = self.list_packages()

        if "/" in identifier:
            author, name = identifier.split("/", 1)
            author = author.strip()
            name = name.strip()
            if not author or not name:
                raise PackageNotFoundError(
                    f"Package identifier '{identifier}' is invalid. Expected format 'author/name'."
                )
            author_cf = author.casefold()
            name_cf = name.casefold()
            for package in packages:
                if package.author.casefold() == author_cf and package.name.casefold() == name_cf:
                    return package
            raise PackageNotFoundError(f"Package '{identifier}' not found in registry")

        name_cf = identifier.casefold()
        matches = [pkg for pkg in packages if pkg.name.casefold() == name_cf]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            slugs = ", ".join(pkg.slug for pkg in matches)
            raise PackageNotFoundError(
                f"Multiple packages share the name '{identifier}'. Use one of: {slugs}"
            )
        raise PackageNotFoundError(f"Package '{identifier}' not found in registry")

    # ------------------------------------------------------------------
    # Fetching artefacts
    # ------------------------------------------------------------------
    def fetch_bytes(self, relative_path: str) -> bytes:
        if not relative_path:
            raise RegistryError("File entries must provide a relative path")
        return self._fetcher(relative_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _load_local(self) -> dict:
        try:
            raw = self._index_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:  # pragma: no cover - defensive path
            raise RegistryError(f"Registry index could not be read: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RegistryError(f"Registry index contains invalid JSON: {exc}") from exc

    def _load_remote(self) -> dict:
        try:
            with urlopen(self._index_url) as response:
                data = response.read()
        except OSError as exc:
            raise RegistryError(f"Unable to download registry index: {exc}") from exc
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RegistryError(f"Registry index contains invalid JSON: {exc}") from exc

    def _fetch_local(self, relative_path: str) -> bytes:
        candidate = (self._base_path / relative_path).resolve()
        try:
            candidate.relative_to(self._base_path.resolve())
        except ValueError as exc:
            raise RegistryError(
                f"Refusing to fetch file outside of the registry root: {relative_path}"
            ) from exc
        if not candidate.is_file():
            raise RegistryError(f"Registry file not found: {relative_path}")
        return candidate.read_bytes()

    def _fetch_remote(self, relative_path: str) -> bytes:
        url = urljoin(self._base_url, relative_path)
        try:
            with urlopen(url) as response:
                return response.read()
        except OSError as exc:
            raise RegistryError(f"Unable to download '{relative_path}': {exc}") from exc