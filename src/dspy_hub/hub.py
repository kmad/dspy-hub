"""High-level SDK helpers for interacting with DSPy Hub registries."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Optional
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


def load_program_from_hub(
    identifier: str,
    program: Any | Callable[[], Any],
    *,
    registry: Optional[str] = None,
    target: Optional[str] = None,
) -> Any:
    """Load a serialized DSPy program from the hub into an instantiated object.

    The ``program`` argument can be an existing DSPy instance or a zero-argument
    factory (e.g. ``lambda: dspy.ChainOfThought(MyModule)``, or a ``functools.partial``)
    that produces one. The helper will fetch the package artifact, write it to a
    temporary location, call ``load`` on the instance, and then return the now-loaded
    object.
    """

    package = load_from_hub(identifier, registry=registry)
    if not package.files:
        raise RegistryError(f"Package '{identifier}' does not contain any files to load")

    instance = _ensure_program_instance(program)
    selected = _select_package_file(package, target)

    with TemporaryDirectory() as tmpdir:
        artifact_path = Path(tmpdir) / Path(selected.target).name
        artifact_path.write_bytes(selected.content)
        loader = getattr(instance, "load", None)
        if not callable(loader):
            raise TypeError(
                "The provided program instance does not expose a callable 'load' method"
            )
        loader(str(artifact_path))

    return instance


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
    The identifier should be just the package name (e.g., 'my-package'), not 'author/package'.
    The author will be determined by the backend from the dev key.
    """

    if not isinstance(package, HubPackage):
        raise TypeError("'package' must be an instance of HubPackage returned by load_from_hub")

    name = package.identifier
    if identifier and identifier != name:
        raise ValueError(
            f"Identifier mismatch: expected '{package.identifier}', got '{identifier}'"
        )

    # Validate that identifier is just a name, not author/name
    if "/" in name:
        raise ValueError(
            "Identifier should be the package name only (e.g., 'my-package'), "
            "not 'author/package'. The author will be determined from your dev key."
        )

    settings = load_settings()
    registry_location = registry or settings.registry

    dev_token = dev_key or os.getenv(DEV_KEY_ENV)
    if not dev_token:
        raise RegistryError(
            "DSPY Hub dev key missing. Set the DSPY_HUB_DEV_KEY environment variable or "
            "pass 'dev_key' explicitly."
        )

    # Merge user-provided metadata with metadata from DSPy saved file
    merged_metadata = {**package.manifest.get("metadata", {}), **(package_metadata or {})}

    payload_manifest = dict(package.manifest)
    payload_manifest["name"] = name
    payload_manifest["version"] = package_metadata.get(
        "version", payload_manifest.get("version", "0.0.0")
    )
    payload_manifest["description"] = package_metadata.get(
        "description", payload_manifest.get("description", "")
    )
    if "tags" in package_metadata:
        payload_manifest["tags"] = package_metadata["tags"]
    payload_manifest["metadata"] = merged_metadata

    files_payload = []
    manifest_files = []
    for hub_file in package.files:
        relative_target = hub_file.target.lstrip("/")
        storage_path = hub_file.source or f"packages/{name}/{relative_target}"
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

    # API endpoint now doesn't include author - backend will determine from dev key
    base_url = registry_location.rsplit("/", 1)[0] + "/"
    endpoint = urljoin(base_url, f"api/packages/{name}")

    request_body = json.dumps(
        {
            "manifest": payload_manifest,
            "metadata": merged_metadata,
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


def save_program_to_hub(
    identifier: str,
    program: Any | Callable[[], Any],
    package_metadata: dict,
    *,
    registry: Optional[str] = None,
    dev_key: Optional[str] = None,
    artifact_name: Optional[str] = None,
) -> dict:
    """Serialize a DSPy program locally and publish it to the hub in one call.

    ``program`` may be an instantiated DSPy module or a zero-argument factory that
    returns one. The helper calls ``save`` under the hood, wraps the resulting
    artifact in a :class:`HubPackage`, and forwards it to :func:`save_to_hub`.
    """

    package = _package_program(identifier, program, artifact_name=artifact_name)
    return save_to_hub(
        identifier,
        package,
        package_metadata,
        registry=registry,
        dev_key=dev_key,
    )


def _default_target(source: str) -> str:
    return source.split("/")[-1]


def _package_program(
    identifier: str,
    program: Any | Callable[[], Any],
    artifact_name: Optional[str] = None,
) -> HubPackage:
    instance = _ensure_program_instance(program)
    saver = getattr(instance, "save", None)
    if not callable(saver):
        raise TypeError(
            "Program must expose a callable 'save(path)' method to publish to the hub"
        )

    # Identifier is now just the package name (no author/)
    name = identifier.strip()
    if not name or "/" in name:
        raise ValueError(
            "Identifier should be the package name only (e.g., 'my-package'), "
            "not 'author/package'. The author will be determined from your dev key."
        )

    artifact_filename = artifact_name or f"{name}.json"

    with TemporaryDirectory() as tmpdir:
        output_path = Path(tmpdir) / artifact_filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        saver(str(output_path))
        content = output_path.read_bytes()

    # Extract metadata from the DSPy saved JSON
    dspy_metadata = {}
    try:
        saved_data = json.loads(content.decode("utf-8"))
        if "metadata" in saved_data and isinstance(saved_data["metadata"], dict):
            dspy_metadata = saved_data["metadata"]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass  # If we can't parse, just skip metadata extraction

    # Detect module type from the saved program structure
    module_type = _detect_module_type(instance)
    if module_type:
        dspy_metadata["module_type"] = module_type

    sha256 = hashlib.sha256(content).hexdigest()
    # Storage path will be determined by backend, but we still need a placeholder
    storage_path = f"packages/{name}/{artifact_filename}"
    hub_file = HubFile(
        source=storage_path,
        target=artifact_filename,
        content=content,
        sha256=sha256,
    )

    manifest = {
        "slug": name,  # Just the name now, author added by backend
        "name": name,
        "files": [
            {"source": storage_path, "target": artifact_filename, "sha256": sha256}
        ],
        "metadata": dspy_metadata,
        "hash": hashlib.sha256(sha256.encode("utf-8")).hexdigest(),
    }

    return HubPackage(identifier=name, manifest=manifest, files=[hub_file])


def _select_package_file(package: HubPackage, target: Optional[str]) -> HubFile:
    if target:
        file_map = package.file_map()
        candidate = file_map.get(target)
        if not candidate:
            basename = target.split("/")[-1]
            candidate = next(
                (hub_file for hub_file in package.files if hub_file.target.endswith(basename)),
                None,
            )
        if candidate:
            return candidate
        raise RegistryError(
            f"Package '{package.identifier}' does not contain an artifact matching '{target}'"
        )
    return package.files[0]


def _ensure_program_instance(program: Any | Callable[[], Any]) -> Any:
    if callable(program) and not hasattr(program, "load"):
        candidate = program()
    else:
        candidate = program
    if not hasattr(candidate, "load"):
        raise TypeError(
            "Program must be an instantiated DSPy object (or factory) exposing 'load(path)'"
        )
    return candidate


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


def _detect_module_type(instance: Any) -> Optional[str]:
    """
    Detect the DSPy module type from an instance.
    Returns a string like 'ChainOfThought', 'Predict', 'ReAct', etc.
    Only supports built-in DSPy modules.
    """
    class_name = instance.__class__.__name__

    # List of known DSPy predictor/module types
    known_types = [
        "Predict",
        "ChainOfThought",
        "ChainOfThoughtWithHint",
        "ProgramOfThought",
        "ReAct",
        "MultiChainComparison",
    ]

    if class_name in known_types:
        return class_name

    # If it's a custom module, we can't auto-detect (return None)
    return None
