"""Command line interface for dspy-hub."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from textwrap import indent

from .config import load_settings
from .exceptions import InstallationError, PackageNotFoundError, RegistryError
from .installer import InstallOptions, Installer
from .repository import PackageRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dspy-hub",
        description="Browse and install DSPy programs from DSPy Hub registries.",
    )
    parser.add_argument(
        "--registry",
        metavar="URL_OR_PATH",
        help="Override the registry index location (defaults to config or bundled sample)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List available packages")
    list_parser.add_argument(
        "--long",
        action="store_true",
        help="Show detailed package descriptions",
    )

    install_parser = subparsers.add_parser("install", help="Install a package")
    install_parser.add_argument(
        "package_identifier",
        help="Identifier of the package to install (e.g. author/name)",
    )
    install_parser.add_argument(
        "--dest",
        default="dspy_packages",
        help="Destination directory for installed files (default: ./dspy_packages)",
    )
    install_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files if they already exist",
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be installed without writing any files",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        registry_location = args.registry or settings.registry
        repository = PackageRepository(registry_location)

        if args.command == "list":
            _handle_list(repository, show_details=args.long)
            return 0

        if args.command == "install":
            _handle_install(
                repository,
                package_identifier=args.package_identifier,
                destination=args.dest,
                force=args.force,
                dry_run=args.dry_run,
            )
            return 0

        parser.error(f"Unknown command: {args.command}")  # pragma: no cover - defensive
    except (RegistryError, InstallationError, PackageNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def _handle_list(repository: PackageRepository, show_details: bool) -> None:
    packages = repository.list_packages()
    if not packages:
        print("No packages available in the registry.")
        return

    if show_details:
        for package in packages:
            print(f"{package.slug} ({package.version})")
            print(f"  Title: {package.name}")
            print(indent(package.description.strip(), prefix="  "))
            if package.tags:
                tags = ", ".join(package.tags)
                print(f"  Tags: {tags}")
            print(f"  Author: {package.author}")
            if package.homepage:
                print(f"  Homepage: {package.homepage}")
            _print_metadata_details(package.raw.get("metadata"))
            print()
        return

    identifier_width = max(len(pkg.slug) for pkg in packages)
    version_width = max(len(pkg.version) for pkg in packages)
    header = (
        f"{'IDENTIFIER'.ljust(identifier_width)}  "
        f"{'VERSION'.ljust(version_width)}  DESCRIPTION"
    )
    print(header)
    print("-" * len(header))
    for package in packages:
        description = package.description.splitlines()[0]
        print(
            f"{package.slug.ljust(identifier_width)}  "
            f"{package.version.ljust(version_width)}  {description}"
        )
        metadata_summary = _build_metadata_summary(package.raw.get("metadata"))
        if metadata_summary:
            print(f"{' ' * (identifier_width + version_width + 4)}{metadata_summary}")


def _handle_install(
    repository: PackageRepository,
    package_identifier: str,
    destination: str,
    force: bool,
    dry_run: bool,
) -> None:
    package = repository.get_package(package_identifier)
    installer = Installer(repository)

    destination_path = Path(destination)
    options = InstallOptions(destination=destination_path, force=force, dry_run=dry_run)

    planned = installer.install(package, options)

    if dry_run:
        print(f"Package: {package.slug} ({package.version})")
        print("The following files would be created:")
        for path in planned:
            print(f"  {path}")
        return

    if not planned:
        print("No files were installed (package may be empty).")
        return

    print(
        f"Installed {package.slug} ({package.version}) "
        f"to {destination_path.resolve()}"
    )
    for path in planned:
        print(f"  {path}")

    post_install_message = package.raw.get("post_install_message")
    if post_install_message:
        print()
        print(post_install_message)


def _print_metadata_details(metadata: dict | None) -> None:
    if not isinstance(metadata, dict):
        return

    program = metadata.get("program")
    if isinstance(program, dict):
        class_path = program.get("class_path") or program.get("class_name")
        if class_path:
            print(f"  Program: {class_path}")
        modules = program.get("modules")
        if isinstance(modules, list) and modules:
            module_list = ", ".join(
                entry.get("class_path", "<unknown>") for entry in modules[:5]
            )
            more = "..." if len(modules) > 5 else ""
            print(f"  Components: {module_list}{more}")

    lm_info = metadata.get("lm")
    if isinstance(lm_info, dict):
        summary = _summarize_lm(lm_info)
        if summary:
            print(f"  LM: {summary}")

    optimizer = metadata.get("optimizer")
    if optimizer:
        optimizer_summary = optimizer if isinstance(optimizer, str) else optimizer.get("name")
        if optimizer_summary:
            print(f"  Optimizer: {optimizer_summary}")

    deps = metadata.get("dependency_versions")
    if isinstance(deps, dict) and deps.get("dspy"):
        print(f"  DSPy Version: {deps['dspy']}")


def _build_metadata_summary(metadata: dict | None) -> str:
    if not isinstance(metadata, dict):
        return ""

    parts = []
    program = metadata.get("program")
    if isinstance(program, dict):
        class_path = program.get("class_path") or program.get("class_name")
        if class_path:
            parts.append(f"program={class_path}")

    lm_info = metadata.get("lm")
    if isinstance(lm_info, dict):
        summary = _summarize_lm(lm_info)
        if summary:
            parts.append(f"lm={summary}")

    optimizer = metadata.get("optimizer")
    if isinstance(optimizer, dict):
        name = optimizer.get("name")
        if name:
            parts.append(f"optimizer={name}")
    elif isinstance(optimizer, str):
        parts.append(f"optimizer={optimizer}")

    return " | ".join(parts)


def _summarize_lm(lm_info: dict) -> str:
    segments = []
    model = lm_info.get("model") or lm_info.get("model_id") or lm_info.get("value")
    if model:
        segments.append(str(model))
    provider = lm_info.get("provider")
    if provider:
        segments.append(f"provider={provider}")
    class_path = lm_info.get("class_path")
    if class_path and class_path not in segments:
        segments.append(class_path)
    return ", ".join(segments)
