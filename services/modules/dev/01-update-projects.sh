#!/bin/sh
#@tags: domain:dev, type:nightly, dep:git, power:ac
set -eu

cleanup() {
    # shellcheck disable=SC1091
    . "$PROJECTS_SCRIPT_DIR/common/unproxy.sh"
}
trap cleanup EXIT

# Get existing projects list (one per line)
EXISTING_PROJECTS=$(python3 "$PROJECTS_SCRIPT_DIR/builder.py" list --no-submodule --simple)

build_projects() {
    _extra_args="$1"
    _projects="$2"

    for proj in $_projects; do
        # Check if project exists in EXISTING_PROJECTS
        if ! echo "$EXISTING_PROJECTS" | grep -Fqx "$proj" > /dev/null 2>&1; then
            continue
        fi

        echo "=> Updating $proj..."
        if python3 "$PROJECTS_SCRIPT_DIR/builder.py" update "$proj"; then
            # Release build (word splitting on _extra_args is intended here)
            # shellcheck disable=SC2086
            python3 "$PROJECTS_SCRIPT_DIR/builder.py" build "$proj" --build-type Release $_extra_args

            # Debug build
            python3 "$PROJECTS_SCRIPT_DIR/builder.py" build "$proj" --build-type Debug
        fi
    done
}

if [ "khronos3d" = "${DOTFILES_CURRENT_PROFILE}" ]; then
    build_projects "--install" "amdvlk"
fi

# shellcheck disable=SC1091
. "$PROJECTS_SCRIPT_DIR/common/proxy.sh"

build_projects "--install-dir $HOME/.local --install" "mesa spirv-headers spirv-tools slang"
build_projects "" "llvm"
