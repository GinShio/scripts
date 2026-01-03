#!/usr/bin/env bash

source $XDG_CONFIG_HOME/workflow/.env
set -uo pipefail

SCRIPTS_DIR="$PROJECTS_SCRIPT_DIR/services/daily"
NIGHTLY_TIMESTAMP=${NIGHTLY_TIMESTAMP:-$(date +%s)}
export NIGHTLY_TIMESTAMP

if [[ ! -d "$SCRIPTS_DIR" ]]; then
	printf '[nightly] scripts directory not found: %s\n' "$SCRIPTS_DIR" >&2
	exit 1
fi

run_script() {
	local script_path=$1
	local script_name
	script_name=$(basename "$script_path")

	if [[ ! -f "$script_path" ]]; then
		printf '[nightly] skip missing script: %s\n' "$script_name" >&2
		return 0
	fi

	printf '[nightly] running %s\n' "$script_name"
	if ! bash "$script_path"; then
		printf '[nightly] %s failed\n' "$script_name" >&2
		return 1
	fi

	return 0
}

status=0
shopt -s nullglob
for script in "$SCRIPTS_DIR"/*.sh; do
	if ! run_script "$script"; then
		status=1
	fi
done
shopt -u nullglob

exit $status
