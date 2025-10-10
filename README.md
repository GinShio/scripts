# Builder CLI

Builder is a Python 3.11 command line utility that turns the documentation under `docs/` into a working implementation. It offers:

- Unified configuration loading from `config/` with project detection, presets, and template variables.
- Branch-aware build orchestration that cooperates with Git, supports automatic stashing, and respects component branches.
- Pluggable build system support with generated command sequences for CMake, Meson, and Bazel.
- Preset inheritance, conditional application, and expression evaluation via `{{ }}` placeholders and `[[ ]]` expressions.

## Usage

Create the expected configuration layout (see `docs/config.md` for details), then run:

```sh
builder build myapp --preset development
```

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

## Development

The project targets Python 3.11 and ships with a small unit test suite. Run tests from the repository root:

```sh
python -m unittest discover -s tests
```

Refer to `docs/build.md`, `docs/config.md`, and `docs/git.md` for the authoritative design documentation that guided this implementation.
