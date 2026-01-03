#!/usr/bin/env bash

source $XDG_CONFIG_HOME/workflow/.env

source $PROJECTS_SCRIPT_DIR/common/proxy.sh
trap "source $PROJECTS_SCRIPT_DIR/common/unproxy.sh" EXIT

exisit_projects=( $(python3 $PROJECTS_SCRIPT_DIR/builder.py list --no-submodule |sed '/^Warning:/d' |tail -n +3 |awk '{if ($4 != "<missing>") print $2}') )

build_projects() {
    local -n extra_args="$1"
    local -n projects="$2"
    for project in ${projects[@]}; do
        if ! [[ "${exisit_projects[*]}" =~ "$project" ]]; then
            continue
        fi
        python3 $PROJECTS_SCRIPT_DIR/builder.py update $project
        if [ $? -eq 0 ]; then
            eval "python3 $PROJECTS_SCRIPT_DIR/builder.py build $project --build-type Release ${extra_args[@]}"
            eval "python3 $PROJECTS_SCRIPT_DIR/builder.py build $project --build-type Debug"
        fi
    done
}

if [[ "khronos3d" = "$DOTFILES_CURRENT_PROFILE" ]]; then
    work_extra_args=(--install)
    work_projects=(amdvlk)
    build_projects work_extra_args work_projects
fi

installable_extra_args=(--install-dir $HOME/.local --install)
installable_projects=(mesa spirv-tools slang)
build_projects installable_extra_args installable_projects

common_extra_args=()
common_projects=(llvm)
build_projects common_extra_args common_projects
