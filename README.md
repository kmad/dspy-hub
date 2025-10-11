## dspy-pkg

`dspy-pkg` is an experimental CLI inspired by the shadcn component registry. It lets you
browse and install curated DSPy programs (agents, pipelines, templates) from a remote
registry. Registries can live in object storage such as Cloudflare R2, GitHub, or any
static hosting service that can serve a JSON manifest and source files.

### Features

- `dspy-pkg list` — inspect the packages exposed by a registry.
- `dspy-pkg install <name>` — copy a package's source files into your project.
- Registry location can be overridden via CLI flags, environment variables, or a
  config file.
- Bundled sample registry demonstrates the expected manifest structure.

### Getting started

```bash
pip install -e .

dspy-pkg list
dspy-pkg install hello-agent --dest ./dspy_components
```

By default the CLI reads from the bundled sample registry located in
`dspy_pkg/sample_registry/index.json`. To point at a custom registry, set the
`DSPY_PKG_REGISTRY` environment variable or pass `--registry`.

```bash
export DSPY_PKG_REGISTRY="https://my-cdn.example.com/registry/index.json"
dspy-pkg list
```

### Registry manifest format

Registries expose a JSON manifest with a top-level `packages` array. Each package entry
must provide:

- `name`: unique identifier.
- `version`: semantic version string.
- `description`: short human description.
- `files`: list describing which source artifacts to install. Each entry requires a
  `source` (relative to the manifest) and optionally a `target` (where it should be
  written inside the destination directory).

Example (abridged):

```json
{
  "packages": [
    {
      "name": "hello-agent",
      "version": "0.1.0",
      "description": "Minimal DSPy agent example",
      "files": [
        {"source": "packages/hello-agent/hello_agent.py", "target": "hello-agent/hello_agent.py"}
      ]
    }
  ]
}
```

### Installation destinations

Packages are copied into a destination folder (default `./dspy_packages`). Each file's
`target` is interpreted relative to that folder. Use `--dest` to override, and `--force`
to overwrite pre-existing files.

```bash
dspy-pkg install hello-agent --dest src/agents --force
```

### Cloudflare R2 backend

The repository includes an optional Cloudflare Worker that exposes the registry
from an R2 bucket. See `cloudflare/registry-worker/README.md` for deployment
instructions and sample metadata.

### Future work

- Package metadata signing / verification.
- Template helpers for wiring agents into an existing DSPy project.
- Richer discovery commands (`search`, `info`, etc.).
- Partial installs (e.g. installing specific components of a package).
