"""High-level SDK helpers for interacting with DSPy Hub registries."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
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
    program: Any | Callable[[], Any] | None = None,
    *,
    version: Optional[str] = None,
    registry: Optional[str] = None,
    target: Optional[str] = None,
) -> Any:
    """Load a serialized DSPy program from the hub.

    By default this helper restores the artifact directly with ``dspy.load`` using
    DSPy's whole-program serialization. For legacy JSON packages—or when you prefer
    to control instantiation—provide an existing DSPy instance or a zero-argument
    factory (e.g. ``lambda: dspy.ChainOfThought(MyModule)``). The helper fetches the
    artifact, materializes it locally, and either calls ``dspy.load`` or invokes the
    instance's ``load`` method depending on the provided arguments.

    Args:
        identifier: Package identifier in 'author/name' format
        program: Optional DSPy program instance or zero-argument factory for legacy artifacts
        version: Optional version string. If not specified, loads latest version.
        registry: Optional custom registry URL
        target: Optional specific file to load from package
    """

    package = load_from_hub(identifier, version=version, registry=registry)
    metadata = package.metadata if isinstance(package.metadata, dict) else {}
    if not package.files:
        raise RegistryError(f"Package '{identifier}' does not contain any files to load")

    selected = _select_package_file(package, target)

    with TemporaryDirectory() as tmpdir:
        artifact_path = Path(tmpdir) / Path(selected.target).name
        artifact_path.write_bytes(selected.content)
        load_target, artifact_kind = _materialize_artifact(artifact_path)

        if program is None:
            if artifact_kind == "legacy_file":
                raise RegistryError(
                    "This package was published with a legacy format. "
                    "Pass a DSPy program instance (or factory) to load it."
                )
            try:
                import dspy  # type: ignore
            except Exception as exc:
                raise RegistryError(
                    "Loading DSPy programs without providing an instance requires the 'dspy' "
                    "package to be installed."
                ) from exc

            loaded_program = dspy.load(str(load_target))
            _validate_program_for_load(identifier, loaded_program, metadata)
            return loaded_program

        instance = _ensure_program_instance(program)
        _validate_program_for_load(identifier, instance, metadata)
        loader = getattr(instance, "load", None)
        if not callable(loader):
            raise TypeError(
                "The provided program instance does not expose a callable 'load' method"
            )
        loader(str(load_target))
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


def delete_package(
    identifier: str,
    *,
    registry: Optional[str] = None,
    dev_key: Optional[str] = None,
    version: Optional[str] = None,
) -> dict:
    """Delete a package (or a single version) from the hub registry."""

    package_name = _normalize_package_name(identifier)

    settings = load_settings()
    registry_location = registry or settings.registry

    dev_token = dev_key or os.getenv(DEV_KEY_ENV)
    if not dev_token:
        raise RegistryError(
            "DSPY Hub dev key missing. Set the DSPY_HUB_DEV_KEY environment variable or "
            "pass 'dev_key' explicitly."
        )

    base_url = registry_location.rsplit("/", 1)[0] + "/"
    version_param = version.strip() if isinstance(version, str) else None
    endpoint = urljoin(base_url, f"api/packages/{package_name}")
    if version_param:
        endpoint = f"{endpoint}?{urlencode({'version': version_param})}"

    request = Request(
        endpoint,
        method="DELETE",
        headers={
            "authorization": f"Bearer {dev_token}",
        },
    )

    try:
        with urlopen(request) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as exc:  # pragma: no cover - network errors
        message = exc.read().decode("utf-8", errors="ignore") or exc.reason
        raise RegistryError(f"Failed to delete package: {message}") from exc
    except URLError as exc:  # pragma: no cover - network errors
        raise RegistryError(f"Failed to reach registry endpoint: {exc}") from exc

    if not response_body:
        return {"success": True}

    try:
        data = json.loads(response_body)
    except json.JSONDecodeError as exc:  # pragma: no cover - unexpected
        raise RegistryError("Registry returned invalid JSON response") from exc

    if not data.get("success") and data.get("error"):
        raise RegistryError(f"Failed to delete package: {data['error']}")

    return data


def save_program_to_hub(
    identifier: str,
    program: Any | Callable[[], Any],
    package_metadata: dict,
    *,
    registry: Optional[str] = None,
    dev_key: Optional[str] = None,
    artifact_name: Optional[str] = None,
    modules_to_serialize: Optional[Sequence[Any]] = None,
) -> dict:
    """Serialize a DSPy program locally and publish it to the hub in one call.

    ``program`` may be an instantiated DSPy module or a zero-argument factory that
    returns one. The helper invokes ``program.save(..., save_program=True)`` to
    capture the full architecture and state, zips the saved directory, and forwards
    the archive to :func:`save_to_hub`. Pass any custom modules needed at load time
    via ``modules_to_serialize`` so DSPy bundles them with the program.
    """

    package = _package_program(
        identifier,
        program,
        artifact_name=artifact_name,
        modules_to_serialize=modules_to_serialize,
    )
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
    modules_to_serialize: Optional[Sequence[Any]] = None,
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

    artifact_filename = artifact_name or f"{name}.zip"
    if not artifact_filename.endswith(".zip"):
        artifact_filename = f"{artifact_filename}.zip"

    with TemporaryDirectory() as tmpdir:
        program_dir = Path(tmpdir) / "program"
        program_dir.mkdir(parents=True, exist_ok=True)
        saver(
            str(program_dir),
            save_program=True,
            modules_to_serialize=modules_to_serialize,
        )

        saved_data = _load_saved_program_metadata(program_dir)
        if isinstance(saved_data, dict) and isinstance(saved_data.get("metadata"), dict):
            dspy_metadata: dict = dict(saved_data["metadata"])
        else:
            dspy_metadata = saved_data if isinstance(saved_data, dict) else {}

        _merge_metadata_missing(
            dspy_metadata,
            _build_program_metadata(instance, saved_data),
        )

        archive_base = Path(tmpdir) / "artifact"
        archive_path = Path(
            shutil.make_archive(
                str(archive_base),
                "zip",
                root_dir=program_dir.parent,
                base_dir=program_dir.name,
            )
        )

        final_archive = archive_path
        if archive_path.name != artifact_filename:
            final_archive = archive_path.with_name(artifact_filename)
            archive_path.rename(final_archive)
        content = final_archive.read_bytes()

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


def _normalize_package_name(identifier: str) -> str:
    slug = (identifier or "").strip()
    if not slug:
        raise RegistryError("Package identifier cannot be empty")

    parts = slug.split("/")
    if len(parts) == 1:
        name = parts[0].strip()
    elif len(parts) == 2 and parts[1].strip():
        name = parts[1].strip()
    else:
        raise RegistryError(
            "Package identifier must be the package name or in the form 'author/name'"
        )

    if not name or "/" in name:
        raise RegistryError("Package name cannot be empty or contain '/' characters")

    return name


def _load_saved_program_metadata(program_dir: Path) -> Optional[dict]:
    metadata_candidates = [
        program_dir / "metadata.json",
        program_dir / "manifest.json",
    ]
    for metadata_file in metadata_candidates:
        if not metadata_file.is_file():
            continue
        try:
            data = json.loads(metadata_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _materialize_artifact(artifact_path: Path) -> Tuple[Path, str]:
    if zipfile.is_zipfile(artifact_path):
        extract_root = artifact_path.parent / "program"
        extract_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(artifact_path) as archive:
            archive.extractall(extract_root)

        program_dir = _locate_program_directory(extract_root)
        if program_dir is None:
            raise RegistryError(
                "Extracted archive does not contain a recognizable DSPy program directory"
            )
        return program_dir, "program_dir"

    return artifact_path, "legacy_file"


def _locate_program_directory(root: Path) -> Optional[Path]:
    if not root.exists():
        return None
    if (root / "metadata.json").is_file() or (root / "manifest.json").is_file():
        return root

    subdirs = sorted(child for child in root.iterdir() if child.is_dir())
    if len(subdirs) == 1:
        candidate = _locate_program_directory(subdirs[0])
        if candidate is not None:
            return candidate
    for subdir in subdirs:
        candidate = _locate_program_directory(subdir)
        if candidate is not None:
            return candidate
    return None


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
    if path.endswith(".zip"):
        return "application/zip"
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
        sanitized = _sanitize_metadata(payload)
        return _normalize_lm_metadata(sanitized)
    return {"value": str(payload)}


def _serialize_lm_instance(lm: Any) -> Optional[dict]:
    if lm is None:
        return None

    data: Dict[str, Any] = {"class_path": _module_class_path(lm)}
    for attr in ("model", "model_name", "model_id"):
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

    normalized = _normalize_lm_metadata(data)
    if not normalized:
        return None
    return normalized


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


_ALLOWED_LM_KEYS = {
    "model",
    "model_name",
    "model_id",
    "value",
    "class_path",
    "kwargs",
    "config",
    "settings",
}


def _normalize_lm_metadata(data: Any) -> Optional[dict]:
    if not isinstance(data, dict):
        return None
    normalized: Dict[str, Any] = {}
    for key in _ALLOWED_LM_KEYS:
        if key not in data:
            continue
        value = data[key]
        if value is None:
            continue
        if isinstance(value, (dict, list)) and not value:
            continue
        normalized[key] = value
    return normalized or None


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

    warnings.warn(
        f"{message}. Ensure your configured LM matches to get consistent behaviour.",
        RuntimeWarning,
        stacklevel=2,
    )
