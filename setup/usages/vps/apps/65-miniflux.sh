#!/bin/sh
#@tags: usage:vps, scope:apps, os:debian, os:ubuntu
# Apps: Miniflux RSS Reader

set -e

echo "Installing Miniflux..."

# Add Miniflux APT repository
if [ ! -f /etc/apt/sources.list.d/miniflux.list ]; then
    echo "deb [trusted=yes] https://repo.miniflux.app/apt/ * *" > /etc/apt/sources.list.d/miniflux.list
    apt update
fi

# Create non-login user miniflux if it doesn't exist
if ! id -u miniflux >/dev/null 2>&1; then
    echo "Creating non-login user miniflux..."
    useradd -r -m -d /var/lib/miniflux -s /usr/sbin/nologin miniflux
fi

# Install Miniflux
DEBIAN_FRONTEND=noninteractive apt -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" install -y miniflux

# Configure PostgreSQL for Miniflux
echo "Configuring PostgreSQL for Miniflux..."
if ! su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='miniflux'\"" | grep -q 1; then
    su - postgres -c "psql -c \"CREATE USER miniflux WITH PASSWORD 'miniflux';\""
fi

if ! su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='miniflux'\"" | grep -q 1; then
    su - postgres -c "psql -c \"CREATE DATABASE miniflux OWNER miniflux;\""
    su - postgres -c "psql -c \"GRANT ALL PRIVILEGES ON DATABASE miniflux TO miniflux;\""
fi

# Enable hstore extension for Miniflux
su - postgres -c "psql -d miniflux -c \"CREATE EXTENSION IF NOT EXISTS hstore;\""
su - postgres -c "psql -d miniflux -c \"ALTER SCHEMA public OWNER TO miniflux;\""
su - postgres -c "psql -d miniflux -c \"UPDATE pg_extension SET extowner = (SELECT oid FROM pg_roles WHERE rolname = 'miniflux') WHERE extname = 'hstore';\""

# Move configuration file to ~miniflux
MINIFLUX_HOME=$(getent passwd miniflux | cut -d: -f6)
if [ -f /etc/miniflux.conf ] && [ ! -f "$MINIFLUX_HOME/miniflux.conf" ]; then
    sudo -u miniflux cp /etc/miniflux.conf "$MINIFLUX_HOME/miniflux.conf"
    chmod 600 "$MINIFLUX_HOME/miniflux.conf"
fi

# Run Miniflux database migrations
echo "Running Miniflux database migrations..."
if [ -f "$MINIFLUX_HOME/miniflux.conf" ]; then
    su - miniflux -s /bin/sh -c "miniflux -c $MINIFLUX_HOME/miniflux.conf -migrate" || true
else
    miniflux -migrate || true
fi

# Update systemd service to run as miniflux user and use config from ~miniflux
mkdir -p /etc/systemd/system/miniflux.service.d
cat > /etc/systemd/system/miniflux.service.d/override.conf <<EOF
[Service]
User=miniflux
Group=miniflux
EnvironmentFile=
EnvironmentFile=$MINIFLUX_HOME/miniflux.conf
EOF
