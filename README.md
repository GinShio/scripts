# Builder CLI

Builder is a Python 3.11 command line utility that turns the documentation under `docs/` into a working implementation. It offers:

- Unified configuration loading from `config/` with project detection, presets, and template variables.
- Branch-aware build orchestration that cooperates with Git, supports automatic stashing, and respects component branches.
- Pluggable build system support with generated command sequences for CMake, Meson, and Bazel.
- Preset inheritance, conditional application, and expression evaluation via `{{ }}` placeholders and `[[ ]]` expressions.

## Setup

### Install (recommended)

Create a virtual environment and install the CLI in editable mode:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

### Run without installing

If you prefer not to install the package, invoke the CLI module directly from the repository root:

```sh
python -m builder.cli build myapp --preset development
```

You can also call the script file explicitly:

```sh
python builder/cli.py build myapp --preset development
```

## Usage

Once the configuration layout is in place (see `docs/config.md`), the installed `builder` command is available. Configuration files may be written in TOML, JSON, or YAML, and the loader will pick the appropriate parser based on the file extension:

```sh
builder build myapp --preset development
```

If you omit presets, the CLI will automatically apply `configs.<build_type>` for single-config generators, or both `configs.debug` and `configs.release` for multi-config generators when those presets are defined.

Enable dry runs to inspect the generated build plan:

```sh
builder build myapp --preset development --dry-run --show-vars
```

Validate the configuration repository:

```sh
builder validate
```

Update a project and its submodules:

```sh
builder update myapp --branch feature-x
```

List the current branch and commit for all configured projects:

```sh
builder list
```

Inspect a single project:

```sh
builder list myapp
```

Reveal presets and dependency relationships in the listing:

```sh
builder list myapp --presets --dependencies
```

## Development

The project targets Python 3.11 and ships with a small unit test suite. Run tests from the repository root:

```sh
python -m unittest discover -s tests
```

Refer to `docs/build.md`, `docs/config.md`, and `docs/git.md` for the authoritative design documentation that guided this implementation.
