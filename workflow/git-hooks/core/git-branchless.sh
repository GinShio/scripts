#!/bin/sh
# Generic Git Branchless Adapter

# Ensure core lib is loaded
. "$HOOKS_DIR/core/lib.sh"

if check_program git-branchless; then
    if [ "$HOOK_NAME" = "reference-transaction" ]; then
        # Avoid canceling the reference transaction in the case that `branchless` fails
        # for whatever reason.
        git branchless hook reference-transaction "$@" || (
        echo 'branchless: Failed to process reference transaction!'
        echo 'branchless: Some events (e.g. branch updates) may have been lost.'
        echo 'branchless: This is a bug. Please report it.'
        )
    else
        git branchless hook "$HOOK_NAME" "$@"
    fi
fi
