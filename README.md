# dspy-hub

`dspy-hub` is the official CLI and Python SDK for browsing, installing, and publishing DSPy
programs. It speaks the same registry format used by [dspyhub.com](https://dspyhub.com) and ships
with a sample registry so you can try the tooling without any external services.

## Installation

```bash
pip install dspy-hub
# or with uv
uv pip install dspy-hub
```

For local development against this repository:

```bash
uv pip install -e .
```

## CLI quick start

The CLI exposes two primary commands: `list` to browse packages and `install` to copy package
artifacts into a local directory.

```bash
dspy-hub list
dspy-hub list --long
dspy-hub install dspy-team/people-extractor --dest ./dspy_packages
```

- Packages are addressed as `<author>/<name>`.
- By default the CLI reads from the bundled sample registry at
  `dspy_hub/sample_registry/index.json`.
- Override the registry with `--registry`, the `DSPY_HUB_REGISTRY` environment variable, or a
  config file (see below).

### Configuration file

On first run the CLI looks for a JSON config at:

- macOS & Linux: `~/.config/dspy-hub/config.json`
- Windows: `%APPDATA%\dspy-hub\config.json`

A minimal config might look like:

```json
{
  "registry": "https://example.com/index.json"
}
```

## Python SDK quick start

Import `dspy_hub` anywhere you need to automate interactions with a registry.

```python
import dspy
import dspy_hub

# Load a published program straight into a DSPy module
module = dspy_hub.load_program_from_hub(
    "dspy-team/people-extractor",
    lambda: dspy.ChainOfThought(PeopleExtraction),
)

# Inspect raw manifests or file payloads
package = dspy_hub.load_from_hub("dspy-team/people-extractor")
print(package.metadata)
```

### Publishing from Python

Publishing requires a developer key provided by the registry operator. Supply it via the
`DSPY_HUB_DEV_KEY` environment variable or the `dev_key` argument.

```python
metadata = {
    "version": "0.1.0",
    "description": "Optimized DSPy program that extracts people names.",
    "tags": ["example", "demo"],
}
dspy_hub.save_program_to_hub("people-extractor", my_program, metadata, registry="https://example.com")
```

The identifier should be the package name only when publishing (`"people-extractor"`); the hub
derives the author namespace from the developer key.

## Environment variables

| Variable            | Purpose                                                                 |
|---------------------|-------------------------------------------------------------------------|
| `DSPY_HUB_REGISTRY` | Override the registry index (`index.json`) used by both CLI and SDK.   |
| `DSPY_HUB_CONFIG`   | Provide an explicit path to the JSON config file.                       |
| `DSPY_HUB_DEV_KEY`  | Developer key required for authenticated publishing workflows.          |

## Bundled sample registry

The package includes a miniature registry under `dspy_hub/sample_registry/`. Point the CLI or SDK
to it (or leave the defaults) to explore the package format without standing up a server. The
sample contains:

- `index.json`: manifest of available packages.
- `packages/<author>/<name>/...`: artifact payloads referenced by the manifest.

## Troubleshooting

- Use `--dry-run` with the `install` command to preview which files would be written.
- Run with `-h` or `--help` to see full CLI usage.
- When diagnosing registry issues, ensure the manifest exposes SHA-256 hashes for each file; the
  installer validates them before writing.
