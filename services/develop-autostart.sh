#!/bin/sh
set -u

# shellcheck disable=SC1091
. "$XDG_CONFIG_HOME/workflow/.env"

SCRIPTS_DIR="$PROJECTS_SCRIPT_DIR/services/autostart"

if [ ! -d "$SCRIPTS_DIR" ]; then
	printf '[autostart] scripts directory not found: %s\n' "$SCRIPTS_DIR" >&2
	exit 1
fi

run_script() {
	_script_path="$1"
	_script_name=$(basename "$_script_path")

	if [ ! -f "$_script_path" ]; then
		printf '[autostart] skip missing script: %s\n' "$_script_name" >&2
		return 0
	fi

	printf '[autostart] running %s\n' "$_script_name"
	# Execute with standard sh since we posix-ified sub-scripts
	if ! /bin/sh "$_script_path"; then
		printf '[autostart] %s failed\n' "$_script_name" >&2
		return 1
	fi

	return 0
}

status=0
for script in "$SCRIPTS_DIR"/*.sh; do
    [ -e "$script" ] || continue

	if ! run_script "$script"; then
		status=1
	fi
done

exit $status
