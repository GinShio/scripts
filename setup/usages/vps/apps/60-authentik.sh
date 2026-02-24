#!/bin/sh
#@tags: usage:vps, scope:apps, os:debian, os:ubuntu, dep:podman
# Apps: Authentik SSO

set -e

echo "Installing Authentik..."

AUTHENTIK_HOME="/var/lib/authentik"

# Create non-login user authentik if it doesn't exist
if ! id -u authentik >/dev/null 2>&1; then
    echo "Creating non-login user authentik..."
    useradd -r -m -d "$AUTHENTIK_HOME" -s /usr/sbin/nologin authentik
else
    # Ensure the user has a nologin shell for security
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

# Migrate podman system for the user to apply subuid/subgid changes
su - authentik -s /bin/sh -c "podman system migrate" || true

# Enable linger for authentik user to run background services
loginctl enable-linger authentik

AUTHENTIK_UID=$(id -u authentik)
cd $AUTHENTIK_HOME

# Enable podman socket for authentik user (optional, for local outposts)
su - authentik -s /bin/sh -c "XDG_RUNTIME_DIR=/run/user/$AUTHENTIK_UID systemctl --user enable --now podman.socket" || true


# Configure PostgreSQL for Authentik
echo "Configuring PostgreSQL for Authentik..."
if ! su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='authentik'\"" | grep -q 1; then
    su - postgres -c "psql -c \"CREATE USER authentik WITH PASSWORD 'authentik';\""
fi

if ! su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='authentik'\"" | grep -q 1; then
    su - postgres -c "psql -c \"CREATE DATABASE authentik OWNER authentik;\""
    su - postgres -c "psql -c \"GRANT ALL PRIVILEGES ON DATABASE authentik TO authentik;\""
fi

# Configure Redis/Valkey for Authentik
echo "Configuring Redis for Authentik..."
# Create an ACL user for Authentik with access to logical database 1 and all keys
if command -v redis-cli >/dev/null 2>&1; then
    redis-cli ACL SETUSER authentik on \>authentik \~\* \&\* +@all -@dangerous
    redis-cli ACL SAVE
fi

# Create systemd service for Authentik
cat > /etc/systemd/system/authentik.service <<EOF
[Unit]
Description=Authentik SSO
Requires=network-online.target
After=network-online.target

[Service]
Type=exec
User=authentik
Group=authentik
WorkingDirectory=$AUTHENTIK_HOME
Environment="XDG_RUNTIME_DIR=/run/user/$AUTHENTIK_UID"
Environment="DOCKER_HOST=unix:///run/user/$AUTHENTIK_UID/podman/podman.sock"
ExecStart=/usr/bin/podman-compose up
ExecStop=/usr/bin/podman-compose down
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable authentik.service
# systemctl start authentik.service # Let the user start it after configuration
