#!/bin/sh
set -u

# shellcheck disable=SC1091
. "$XDG_CONFIG_HOME/workflow/.env"

SCRIPTS_DIR="$PROJECTS_SCRIPT_DIR/services/daily"
NIGHTLY_TIMESTAMP=${NIGHTLY_TIMESTAMP:-$(date +%s)}
export NIGHTLY_TIMESTAMP

if [ ! -d "$SCRIPTS_DIR" ]; then
	printf '[nightly] scripts directory not found: %s\n' "$SCRIPTS_DIR" >&2
	exit 1
fi

run_script() {
	_script_path="$1"
	# basename logic using parameter expansion can remove external dependency,
	# but basename utility is standard posix.
	_script_name=$(basename "$_script_path")

	if [ ! -f "$_script_path" ]; then
		printf '[nightly] skip missing script: %s\n' "$_script_name" >&2
		return 0
	fi

	printf '[nightly] running %s\n' "$_script_name"
	# Execute with standard sh since we posix-ified sub-scripts
	if ! /bin/sh "$_script_path"; then
		printf '[nightly] %s failed\n' "$_script_name" >&2
		return 1
	fi

	return 0
}

status=0
for script in "$SCRIPTS_DIR"/*.sh; do
    # Handle the case where the glob matches nothing (literal string '*.sh')
    [ -e "$script" ] || continue

	if ! run_script "$script"; then
		status=1
	fi
done

exit $status
