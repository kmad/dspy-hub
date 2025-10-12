"""High-level SDK helpers for interacting with DSPy Hub registries."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Optional, Tuple
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
    version: Optional[str] = None,
    registry: Optional[str] = None,
) -> HubPackage:
    """Fetch package metadata and contents from the configured registry.

    Args:
        identifier: Package identifier in 'author/name' format
        version: Optional version string. If not specified, loads latest version.
        registry: Optional custom registry URL
    """

    if not identifier or "/" not in identifier:
        raise PackageNotFoundError(
            "Package identifier must be provided in the form 'author/name'"
        )

    settings = load_settings()
    registry_location = registry or settings.registry
    repository = PackageRepository(registry_location)

    # Append version to identifier if specified
    lookup_id = f"{identifier}/{version}" if version else identifier
    package = repository.get_package(lookup_id)

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
    version: Optional[str] = None,
    registry: Optional[str] = None,
    target: Optional[str] = None,
) -> Any:
    """Load a serialized DSPy program from the hub into an instantiated object.

    The ``program`` argument can be an existing DSPy instance or a zero-argument
    factory (e.g. ``lambda: dspy.ChainOfThought(MyModule)``, or a ``functools.partial``)
    that produces one. The helper will fetch the package artifact, write it to a
    temporary location, call ``load`` on the instance, and then return the now-loaded
    object.

    Args:
        identifier: Package identifier in 'author/name' format
        program: DSPy program instance or factory function
        version: Optional version string. If not specified, loads latest version.
        registry: Optional custom registry URL
        target: Optional specific file to load from package
    """

    package = load_from_hub(identifier, version=version, registry=registry)
    if not package.files:
        raise RegistryError(f"Package '{identifier}' does not contain any files to load")

    instance = _ensure_program_instance(program)
    _validate_program_for_load(identifier, instance, package.metadata)
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
    saved_data: dict | None = None
    dspy_metadata: dict = {}
    try:
        saved_data = json.loads(content.decode("utf-8"))
        if "metadata" in saved_data and isinstance(saved_data["metadata"], dict):
            dspy_metadata = saved_data["metadata"]
    except (json.JSONDecodeError, UnicodeDecodeError):
        saved_data = None  # If we can't parse, just skip metadata extraction

    _merge_metadata_missing(
        dspy_metadata,
        _build_program_metadata(instance, saved_data),
    )

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


def _module_class_path(obj: Any) -> str:
    cls = obj.__class__
    module = getattr(cls, "__module__", "")
    qualname = getattr(cls, "__qualname__", cls.__name__)
    return f"{module}.{qualname}".strip(".")


def _build_program_metadata(instance: Any, saved_data: dict | None) -> dict:
    program_info: dict = {
        "class_name": instance.__class__.__name__,
        "class_path": _module_class_path(instance),
    }

    module_inventory = _collect_module_inventory(instance)
    if module_inventory:
        program_info["modules"] = module_inventory

    extras: dict = {"program": program_info}

    optimizer_info = _extract_optimizer_metadata(saved_data)
    if optimizer_info:
        extras.setdefault("optimizer", optimizer_info)

    lm_info = _extract_lm_metadata(instance, saved_data)
    if lm_info:
        extras.setdefault("lm", lm_info)

    extras.setdefault("module_type", program_info["class_path"])
    return extras


def _collect_module_inventory(instance: Any) -> List[dict]:
    inventory: List[dict] = []
    inventory.append({"name": "__root__", "class_path": _module_class_path(instance)})

    try:
        import dspy  # type: ignore

        ModuleBase = getattr(dspy, "Module", None)
    except Exception:  # pragma: no cover - optional dependency
        ModuleBase = None

    for attr, value in sorted(vars(instance).items()):
        if value is instance:
            continue
        if ModuleBase is not None and isinstance(value, ModuleBase):
            inventory.append({"name": attr, "class_path": _module_class_path(value)})
            continue
        if hasattr(value, "load") and hasattr(value, "save"):
            inventory.append({"name": attr, "class_path": _module_class_path(value)})

    seen: set[Tuple[str, str]] = set()
    unique_inventory: List[dict] = []
    for entry in inventory:
        key = (entry["name"], entry["class_path"])
        if key in seen:
            continue
        seen.add(key)
        unique_inventory.append(entry)
    return unique_inventory


def _extract_optimizer_metadata(saved_data: dict | None) -> Optional[dict]:
    if not isinstance(saved_data, dict):
        return None
    candidates = [
        saved_data.get("optimizer"),
        saved_data.get("metadata", {}).get("optimizer"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return _sanitize_metadata(candidate)
        if isinstance(candidate, str) and candidate:
            return {"name": candidate}
    return None


def _extract_lm_metadata(instance: Any, saved_data: dict | None) -> Optional[dict]:
    lm_payload = None
    if isinstance(saved_data, dict):
        for path in (["predict", "lm"], ["lm"], ["metadata", "lm"]):
            lm_payload = _dig(saved_data, path)
            if lm_payload:
                break

    serialized = _serialize_lm_payload(lm_payload)
    if serialized:
        return serialized

    lm_instance = getattr(instance, "lm", None)
    serialized = _serialize_lm_instance(lm_instance)
    if serialized:
        return serialized

    try:
        import dspy  # type: ignore

        lm_from_settings = getattr(getattr(dspy, "settings", object()), "lm", None)
        return _serialize_lm_instance(lm_from_settings)
    except Exception:  # pragma: no cover - optional dependency
        return None


def _serialize_lm_payload(payload: Any) -> Optional[dict]:
    if payload is None:
        return None
    if isinstance(payload, dict):
        if not payload:
            return None
        return _sanitize_metadata(payload)
    return {"value": str(payload)}


def _serialize_lm_instance(lm: Any) -> Optional[dict]:
    if lm is None:
        return None

    data: Dict[str, Any] = {"class_path": _module_class_path(lm)}
    for attr in ("model", "model_name", "model_id", "provider", "api_base"):
        value = getattr(lm, attr, None)
        if value is not None:
            data[attr] = _sanitize_metadata(value)

    for attr in ("kwargs", "config", "settings"):
        value = getattr(lm, attr, None)
        if value:
            data[attr] = _sanitize_metadata(value)

    # Avoid empty dict when no additional metadata is available.
    if len(data) == 1 and data["class_path"] == "builtins.object":
        return None

    return data


def _sanitize_metadata(value: Any, depth: int = 0, max_depth: int = 4) -> Any:
    if depth > max_depth:
        return "...(truncated)..."
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            sanitized[str(key)] = _sanitize_metadata(item, depth + 1, max_depth)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(item, depth + 1, max_depth) for item in list(value)]
    return str(value)


def _dig(data: dict, path: List[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _merge_metadata_missing(target: dict, extra: dict) -> None:
    for key, value in extra.items():
        if key not in target or target[key] is None:
            target[key] = value
            continue
        if isinstance(target[key], dict) and isinstance(value, dict):
            _merge_metadata_missing(target[key], value)


def _validate_program_for_load(identifier: str, instance: Any, metadata: dict) -> None:
    if not isinstance(metadata, dict):
        return

    expected_program = metadata.get("program") if isinstance(metadata.get("program"), dict) else {}
    expected_class_path = expected_program.get("class_path") or metadata.get("module_type")
    if expected_class_path:
        actual_class_path = _module_class_path(instance)
        if actual_class_path != expected_class_path:
            raise RegistryError(
                f"Package '{identifier}' expects program '{expected_class_path}', "
                f"but provided '{actual_class_path}'. Pass a matching factory."
            )

    dependency_versions = metadata.get("dependency_versions")
    if isinstance(dependency_versions, dict):
        _warn_on_dependency_mismatch(dependency_versions)

    lm_requirements = metadata.get("lm")
    if isinstance(lm_requirements, dict):
        _warn_on_lm_requirements(lm_requirements)

    setattr(instance, "_dspy_hub_metadata", metadata)


def _warn_on_dependency_mismatch(required: dict) -> None:
    required_dspy = required.get("dspy")
    if not required_dspy:
        return
    try:
        import dspy  # type: ignore

        installed = getattr(dspy, "__version__", None)
    except Exception:  # pragma: no cover - optional dependency
        installed = None
    if installed and installed != required_dspy:
        warnings.warn(
            f"This program was optimized with dspy=={required_dspy}, "
            f"but you are running dspy=={installed}. Behaviour may differ.",
            RuntimeWarning,
            stacklevel=2,
        )


def _warn_on_lm_requirements(requirements: dict) -> None:
    model = requirements.get("model") or requirements.get("model_id") or requirements.get("value")
    if not model:
        return

    message = f"The saved program expects LM '{model}'"
    provider = requirements.get("provider")
    if provider:
        message += f" from provider '{provider}'"

    warnings.warn(
        f"{message}. Ensure your configured LM matches to get consistent behaviour.",
        RuntimeWarning,
        stacklevel=2,
    )
