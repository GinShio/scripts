#!/bin/sh
#@tags: usage:vps, scope:apps, os:debian, os:ubuntu
# Apps: Miniflux RSS Reader
# Prerequisites: dotdrop must have already deployed miniflux.conf and miniflux_admin_password

set -e

echo "Installing Miniflux..."

MINIFLUX_HOME="/var/lib/miniflux"

# Ensure dotdrop has deployed the config before we proceed
if [ ! -f "$MINIFLUX_HOME/miniflux.conf" ]; then
    echo "Error: $MINIFLUX_HOME/miniflux.conf not found."
    echo "Run 'dotdrop install -p vps' first to deploy configuration files."
    exit 1
fi
if [ ! -f "$MINIFLUX_HOME/miniflux_admin_password" ]; then
    echo "Error: $MINIFLUX_HOME/miniflux_admin_password not found."
    echo "Run 'dotdrop install -p vps' first to deploy configuration files."
    exit 1
fi

# Read the generated DB password from the deployed config
# DATABASE_URL format: user=miniflux password=<pass> dbname=miniflux sslmode=disable host=127.0.0.1
DB_PASS=$(grep '^DATABASE_URL=' "$MINIFLUX_HOME/miniflux.conf" \
    | sed 's/.*[[:space:]]password=\([^[:space:]]*\).*/\1/')

if [ -z "$DB_PASS" ]; then
    echo "Error: Could not extract password from DATABASE_URL in $MINIFLUX_HOME/miniflux.conf"
    exit 1
fi

# Add Miniflux APT repository with GPG verification (official method)
if [ ! -f /usr/share/keyrings/miniflux-archive-keyring.gpg ]; then
    echo "Adding Miniflux APT repository..."
    curl -fsSL https://repo.miniflux.app/apt/public.gpg \
        | gpg --dearmor -o /usr/share/keyrings/miniflux-archive-keyring.gpg
    echo 'deb [signed-by=/usr/share/keyrings/miniflux-archive-keyring.gpg] https://repo.miniflux.app/apt/ * *' \
        > /etc/apt/sources.list.d/miniflux.list
    apt update
fi

# Create non-login system user miniflux if it doesn't exist
if ! id -u miniflux >/dev/null 2>&1; then
    echo "Creating non-login user miniflux..."
    useradd -r -m -d "$MINIFLUX_HOME" -s /usr/sbin/nologin miniflux
fi

# Install Miniflux (package no longer auto-configures via /etc/miniflux.conf since we use ~miniflux)
DEBIAN_FRONTEND=noninteractive apt -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" install -y miniflux

# Configure PostgreSQL for Miniflux
echo "Configuring PostgreSQL for Miniflux..."
if ! su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='miniflux'\"" | grep -q 1; then
    su - postgres -c "psql -c \"CREATE USER miniflux WITH PASSWORD '$DB_PASS';\""
else
    # Update password in case it was rotated
    su - postgres -c "psql -c \"ALTER USER miniflux WITH PASSWORD '$DB_PASS';\""
fi

if ! su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='miniflux'\"" | grep -q 1; then
    su - postgres -c "psql -c \"CREATE DATABASE miniflux OWNER miniflux;\""
fi
su - postgres -c "psql -c \"GRANT ALL PRIVILEGES ON DATABASE miniflux TO miniflux;\""

# Enable hstore extension (required by Miniflux) and fix schema ownership (required for PG 15+)
su - postgres -c "psql -d miniflux -c \"CREATE EXTENSION IF NOT EXISTS hstore;\""
su - postgres -c "psql -d miniflux -c \"ALTER SCHEMA public OWNER TO miniflux;\""

# Run Miniflux database migrations as the miniflux user
echo "Running Miniflux database migrations..."
su - miniflux -s /bin/sh -c "miniflux -config-file $MINIFLUX_HOME/miniflux.conf -migrate"

# The miniflux drop-in is deployed by dotdrop (system/systemd/service/miniflux.service.d/override.conf).
# Ensure it is present before reloading:
if [ ! -f /etc/systemd/system/miniflux.service.d/override.conf ]; then
    echo "Error: /etc/systemd/system/miniflux.service.d/override.conf not found."
    echo "Run 'dotdrop install -p vps' first to deploy systemd unit files."
    exit 1
fi

systemctl daemon-reload
echo "Miniflux setup complete. Enable and start with: systemctl enable --now miniflux"
