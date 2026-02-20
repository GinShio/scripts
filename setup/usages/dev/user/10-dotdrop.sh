#!/bin/sh
#@tags: usage:dev, scope:user
# User: Dotdrop & Secrets

# Ensure ~/.local/bin is in PATH for this session (where pipx puts binaries)
export PATH="$HOME/.local/bin:$PATH"

DOTFILES_ROOT_PATH="${DOTFILES_ROOT_PATH:-$PROJECTS_SCRIPT_DIR/../dotfiles}"
if [ ! -d "$DOTFILES_ROOT_PATH" ]; then
    echo "Error: Missing dotfiles directory at ${DOTFILES_ROOT_PATH}"
    exit 1
fi

# Attempt to decrypt secrets if credentials are present
# Convert profile to upper case for variable lookup
PROFILE_UPPER=$(echo "$SETUP_PROFILE" | tr '[:lower:]' '[:upper:]')
TRANSCRYPT_VAR_NAME="TRANSCRYPT_${PROFILE_UPPER}_PASSWORD"

# Check if the variable is set (using indirect reference)
eval "_val=\${$TRANSCRYPT_VAR_NAME:-}"
if [ -n "$_val" ]; then
    echo "Info: Found credentials for profile '$SETUP_PROFILE'. Configuring encryption..."
    (
        cd "$DOTFILES_ROOT_PATH" || exit
        # Install filters for the specific profile context
        python3 "$PROJECTS_SCRIPT_DIR/workflow/transcrypt.py" -c "$SETUP_PROFILE" install

        # Force a hard reset to re-checkout files through the newly installed smudge filter
        # This ensures 'secret.yaml' and others are decrypted on disk
        git rm -rf secret/ && git reset --hard HEAD
    )
fi

if command -v dotdrop >/dev/null 2>&1; then
    echo "Running Dotdrop..."
    # User config
    yes | dotdrop install -c "$DOTFILES_ROOT_PATH/dotfiles.yaml" -p "$SETUP_PROFILE"

    # Secrets config (if decrypted)
    if [ -n "$_val" ] && [ -f "$DOTFILES_ROOT_PATH/secret.yaml" ]; then
         echo "Installing secrets from secret.yaml..."
         yes | dotdrop install -c "$DOTFILES_ROOT_PATH/secret.yaml" -p "$SETUP_PROFILE"
    fi

    # System config (requires SUDO_ASKPASS or cached sudo)
    yes | sudo -AE env "HOME=$HOME" "$(command -v dotdrop)" install -c "$DOTFILES_ROOT_PATH/system.yaml" -p "$SETUP_PROFILE"
else
    echo "Error: dotdrop not found in PATH ($PATH). Did phase_apps.sh run?"
    exit 1
fi
