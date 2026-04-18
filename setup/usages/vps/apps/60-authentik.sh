#!/bin/sh
#@tags: usage:vps, scope:apps, os:debian, os:ubuntu, dep:podman, dep:podman-compose
# Apps: Authentik SSO
# Prerequisites: dotdrop must have already deployed $AUTHENTIK_HOME/.env

set -e

echo "Installing Authentik..."

AUTHENTIK_HOME="/var/lib/authentik"

# Ensure dotdrop has deployed the .env before we proceed
if [ ! -f "$AUTHENTIK_HOME/.env" ]; then
    echo "Error: $AUTHENTIK_HOME/.env not found."
    echo "Run 'dotdrop install -p vps' first to deploy configuration files."
    exit 1
fi

# Read the generated DB password from the deployed .env (rendered by dotdrop)
PG_PASS=$(grep '^PG_PASS=' "$AUTHENTIK_HOME/.env" | cut -d= -f2-)

if [ -z "$PG_PASS" ]; then
    echo "Error: Could not read PG_PASS from $AUTHENTIK_HOME/.env"
    exit 1
fi

# Create non-login system user authentik if it doesn't exist
if ! id -u authentik >/dev/null 2>&1; then
    echo "Creating non-login user authentik..."
    useradd -r -m -d "$AUTHENTIK_HOME" -s /usr/sbin/nologin authentik
else
    usermod -s /usr/sbin/nologin authentik
fi

# Ensure subuid and subgid are configured for rootless podman
if ! grep -q "^authentik:" /etc/subuid; then
    echo "Configuring subuid for authentik..."
    usermod --add-subuids 100000-165535 authentik
fi
if ! grep -q "^authentik:" /etc/subgid; then
    echo "Configuring subgid for authentik..."
    usermod --add-subgids 100000-165535 authentik
fi

# Migrate podman storage/network after subuid/subgid changes
su - authentik -s /bin/sh -c "podman system migrate" || true

# Enable linger so authentik's user units survive logout
loginctl enable-linger authentik

AUTHENTIK_UID=$(id -u authentik)

# Enable podman socket for the authentik user (required for local outpost integration)
su - authentik -s /bin/sh -c "XDG_RUNTIME_DIR=/run/user/$AUTHENTIK_UID systemctl --user enable --now podman.socket" || true

# Ensure media/certs/custom-templates dirs exist with correct ownership
# (worker runs as root inside container, maps to authentik UID on host via user namespace)
install -d -m 750 -o authentik -g authentik \
    "$AUTHENTIK_HOME/media" \
    "$AUTHENTIK_HOME/certs" \
    "$AUTHENTIK_HOME/custom-templates"

# Configure PostgreSQL for Authentik
echo "Configuring PostgreSQL for Authentik..."
if ! su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='authentik'\"" | grep -q 1; then
    su - postgres -c "psql -c \"CREATE USER authentik WITH PASSWORD '$PG_PASS';\""
else
    # Update password in case it was rotated
    su - postgres -c "psql -c \"ALTER USER authentik WITH PASSWORD '$PG_PASS';\""
fi

if ! su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='authentik'\"" | grep -q 1; then
    su - postgres -c "psql -c \"CREATE DATABASE authentik OWNER authentik;\""
fi
# Ensure privileges (idempotent)
su - postgres -c "psql -c \"GRANT ALL PRIVILEGES ON DATABASE authentik TO authentik;\""

# Redis is managed as a sidecar container inside docker-compose.yml.
# No host Redis configuration required for Authentik.

# The authentik.service unit is deployed by dotdrop (system/systemd/service/authentik.service).
# Ensure it is present before enabling:
if [ ! -f /etc/systemd/system/authentik.service ]; then
    echo "Error: /etc/systemd/system/authentik.service not found."
    echo "Run 'dotdrop install -p vps' first to deploy systemd unit files."
    exit 1
fi

systemctl daemon-reload
systemctl enable authentik.service
# Start manually after verifying configuration:
#   systemctl start authentik.service
#   journalctl -u authentik.service -f
