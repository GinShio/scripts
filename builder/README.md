# Builder CLI

Branch-aware build orchestration for multi-project workspaces. Builder reads declarative configuration bundles and turns them into reproducible Git-aware build plans with rich preset support.

## Highlights

- 🔁 **Branch-smart workflows** – switch branches safely with automatic stashing and component-branch overrides.
- 🧩 **Preset inheritance** – compose build variants with layered presets, expressions (`[[ ]]`) and templated variables (`{{ }}`).
- 🛠️ **Multiple toolchains** – generate commands for CMake, Meson, Bazel, custom scripts, and pass through extra arguments.
- 🧱 **Config layering** – merge configuration directories from the repo, environment, or CLI, ideal for per-user overlays.
- 📋 **Unified CLI** – `build`, `update`, `list`, and `validate` live under a single command with consistent ergonomics.

## Quick start

1. **Create an environment and install locally**
	```sh
	python3 -m venv .venv
	. .venv/bin/activate
	pip install -e .
	```

2. **Describe your workspace** under `config/` (see [Configuration](#configuration-layout)). Each project gets one file: TOML, JSON, or YAML are all supported.

3. **Run the CLI**
	```sh
	builder build myapp --preset development
	```

Prefer not to install? Run from the repository root instead:

```sh
python -m builder.cli build myapp --preset development
# or
python builder.py build myapp --preset development
```

## Configuration layout

Builder looks for configuration bundles in layers. The lookup order is:

1. `config/` in the workspace root.
2. Paths listed in `BUILDER_CONFIG_DIR` (use your OS path separator).
3. Each `-C PATH` / `--config-dir PATH` passed on the command line.

Later entries override earlier ones when they define the same file stem. A minimal directory might look like:

```text
config/
├── config.toml            # Global defaults (optional)
├── presets/...
└── projects/
	 ├── myapp.toml
	 └── tools.toml
```

Key ideas:

- One project file per name. Builder automatically chooses the parser from the extension.
- Omit `build_dir` for tracking-only dependencies; they will still be validated but skip build steps.
- Presets can inherit (`extends = [...]`), gate on conditions (`condition = "[[ ... ]]"`), and introduce environment variables or build definitions.

See `docs/config.md` for the full schema and advanced templating rules.

## CLI overview

Global options:
- `-C PATH` / `--config-dir PATH` – append another configuration source (repeatable; respects path separators).

### `builder build`

Configure and execute a project's build plan. Highlights:

- `-p NAME` / `--preset NAME[,NAME]` – apply one or more presets (repeat flag for readability).
- `-b NAME` / `--branch NAME` – override the branch for the run (project + component repos).
- `-n` / `--dry-run` – print actions without running them; combine with `--show-vars` to inspect the resolved context.
- `-G NAME` / `--generator NAME` – force a specific generator (e.g., Ninja, Visual Studio).
- `-B TYPE` / `--build-type TYPE` – override the build type (Debug/Release/...).
- `-t TARGET` / `--target TARGET` – build a specific target when supported by the backend.
- `--config-only`, `--build-only`, `--reconfig` – control orchestration stages.
- `-X[scope],ARG` or `--extra-config-args/--extra-build-args` – pass through extra flags.
- `-DNAME=VALUE` / `--definition NAME=VALUE` – inject temporary build definitions without editing configuration files.
- `-T NAME` / `--toolchain NAME` – select a toolchain (e.g., gcc, clang, msvc).
- `--install`, `--install-dir`, `--no-switch-branch` – tailor the build and Git behavior.

### `builder update`

Clone or refresh repositories (and optional component directories) defined in the configuration. Supports:

- `-b NAME` / `--branch NAME` to switch during the update.
- `-s STRATEGY` / `--submodule {default,latest,skip}` to pick a submodule strategy.
- `-n` / `--dry-run` to preview Git commands.

### `builder list`

Summarize repository state across projects. Add-ons:

- `--presets` to show default presets.
- `--dependencies` to include dependency chains.
- `--url` to include remote URLs.
- `--no-switch-branch` to avoid temporary checkouts when gathering metadata.

### `builder validate`

Validate the configuration store. Provide a project name to narrow the check:

```sh
builder validate            # validate everything
builder validate myapp      # validate only "myapp"
```

## Development

Builder targets Python 3.11+. Run the test suite from the repository root:

```sh
python -m unittest discover -s builder/tests
```

The repository uses a `src/` layout; editable installs (`pip install -e .`) and the helper script `builder.py` expose the CLI without tinkering with `PYTHONPATH`.

## Documentation

- `docs/config.md` – configuration schema, layering, and templating.
- `docs/build.md` – build engine behaviors and command reference.
- `docs/git.md` – Git automation, update flow, and submodule strategies.

Contributions that keep the docs aligned with the CLI behavior are highly appreciated.
