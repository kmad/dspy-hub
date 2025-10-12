"""High-level SDK helpers for interacting with DSPy Hub registries."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .config import load_settings
from .exceptions import PackageNotFoundError, RegistryError
from .repository import PackageRepository


DEV_KEY_ENV = "DSPY_HUB_DEV_KEY"


@dataclass(slots=True)
class HubFile:
    """Represents a file belonging to a hub package."""

    source: str
    target: str
    content: bytes
    sha256: str

    def as_payload(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "path": self.target,
            "sha256": self.sha256,
            "content": base64.b64encode(self.content).decode("ascii"),
        }


@dataclass(slots=True)
class HubPackage:
    """Materialized package pulled from the hub."""

    identifier: str
    manifest: dict
    files: List[HubFile]

    def file_map(self) -> Dict[str, HubFile]:
        return {hub_file.target: hub_file for hub_file in self.files}

    @property
    def metadata(self) -> dict:
        data = self.manifest.get("metadata")
        return data if isinstance(data, dict) else {}


def load_from_hub(
    identifier: str,
    *,
    registry: Optional[str] = None,
) -> HubPackage:
    """Fetch package metadata and contents from the configured registry."""

    if not identifier or "/" not in identifier:
        raise PackageNotFoundError(
            "Package identifier must be provided in the form 'author/name'"
        )

    settings = load_settings()
    registry_location = registry or settings.registry
    repository = PackageRepository(registry_location)
    package = repository.get_package(identifier)

    files: List[HubFile] = []
    manifest = dict(package.raw)

    updated_files: List[dict] = []
    for file_spec in package.files:
        source = file_spec.get("source")
        target = file_spec.get("target") or _default_target(source)
        content = repository.fetch_bytes(source)
        sha256 = hashlib.sha256(content).hexdigest()

        files.append(HubFile(source=source, target=target, content=content, sha256=sha256))
        sanitized_entry = dict(file_spec)
        sanitized_entry["target"] = target
        sanitized_entry["sha256"] = sha256
        updated_files.append(sanitized_entry)

    manifest["files"] = updated_files
    manifest.setdefault("author", identifier.split("/", 1)[0])
    manifest.setdefault("name", identifier.split("/", 1)[1])
    if not isinstance(manifest.get("metadata"), dict):
        manifest["metadata"] = {}
    if files:
        manifest["hash"] = hashlib.sha256(
            "::".join(hub_file.sha256 for hub_file in files).encode("utf-8")
        ).hexdigest()
    manifest["slug"] = identifier

    return HubPackage(identifier=identifier, manifest=manifest, files=files)


def save_to_hub(
    identifier: str,
    package: HubPackage,
    package_metadata: dict,
    *,
    registry: Optional[str] = None,
    dev_key: Optional[str] = None,
) -> dict:
    """Publish a package to the hub registry.

    Requires a developer key (set via ``DSPY_HUB_DEV_KEY`` or ``dev_key``).
    """

    if not isinstance(package, HubPackage):
        raise TypeError("'package' must be an instance of HubPackage returned by load_from_hub")

    slug = package.identifier
    if identifier and identifier != slug:
        raise ValueError(
            f"Identifier mismatch: expected '{package.identifier}', got '{identifier}'"
        )

    author, name = _split_identifier(slug)

    settings = load_settings()
    registry_location = registry or settings.registry

    dev_token = dev_key or os.getenv(DEV_KEY_ENV)
    if not dev_token:
        raise RegistryError(
            "DSPY Hub dev key missing. Set the DSPY_HUB_DEV_KEY environment variable or "
            "pass 'dev_key' explicitly."
        )

    payload_manifest = dict(package.manifest)
    payload_manifest["author"] = author
    payload_manifest["name"] = name
    payload_metadata = {**(package_metadata or {})}
    payload_manifest["version"] = payload_metadata.get(
        "version", payload_manifest.get("version", "0.0.0")
    )
    payload_manifest["description"] = payload_metadata.get(
        "description", payload_manifest.get("description", "")
    )
    if "tags" in payload_metadata:
        payload_manifest["tags"] = payload_metadata["tags"]
    payload_manifest["metadata"] = payload_metadata

    files_payload = []
    manifest_files = []
    for hub_file in package.files:
        relative_target = hub_file.target.lstrip("/")
        if relative_target.startswith(f"{author}/"):
            relative_target = relative_target[len(author) + 1 :]
        if not relative_target:
            relative_target = hub_file.target.lstrip("/") or hub_file.target
        storage_path = hub_file.source or f"packages/{author}/{name}/{relative_target}"
        manifest_files.append(
            {
                "source": storage_path,
                "target": hub_file.target,
                "sha256": hub_file.sha256,
            }
        )
        files_payload.append(
            {
                "path": relative_target,
                "target": hub_file.target,
                "sha256": hub_file.sha256,
                "content": base64.b64encode(hub_file.content).decode("ascii"),
                "contentType": _guess_mime(hub_file.target),
            }
        )

    payload_manifest["files"] = manifest_files

    base_url = registry_location.rsplit("/", 1)[0] + "/"
    endpoint = urljoin(base_url, f"api/packages/{author}/{name}")

    request_body = json.dumps(
        {
            "manifest": payload_manifest,
            "metadata": payload_metadata,
            "files": files_payload,
        }
    ).encode("utf-8")

    request = Request(
        endpoint,
        data=request_body,
        method="PUT",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {dev_token}",
        },
    )

    try:
        with urlopen(request) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as exc:  # pragma: no cover - network errors
        message = exc.read().decode("utf-8", errors="ignore") or exc.reason
        raise RegistryError(f"Failed to publish package: {message}") from exc
    except URLError as exc:  # pragma: no cover - network errors
        raise RegistryError(f"Failed to reach registry endpoint: {exc}") from exc

    try:
        data = json.loads(response_body)
    except json.JSONDecodeError as exc:  # pragma: no cover - unexpected
        raise RegistryError("Registry returned invalid JSON response") from exc

    return data


def _default_target(source: str) -> str:
    return source.split("/")[-1]


def _split_identifier(identifier: str) -> tuple[str, str]:
    if "/" not in identifier:
        raise PackageNotFoundError(
            "Package identifier must be provided in the form 'author/name'"
        )
    author, name = identifier.split("/", 1)
    if not author or not name:
        raise PackageNotFoundError(
            "Package identifier must be provided in the form 'author/name'"
        )
    return author, name


def _guess_mime(path: str) -> str:
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".py"):
        return "text/x-python"
    if path.endswith(".md"):
        return "text/markdown"
    if path.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"