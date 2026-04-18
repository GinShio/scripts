#!/bin/sh
#@tags: usage:vps, scope:services, os:debian, os:ubuntu
# Services: Enable and start VPS web services

set -e

systemctl daemon-reload

# Nginx
if systemctl list-unit-files | grep -q nginx.service; then
    echo "Enabling Nginx..."
    systemctl enable --now nginx.service
fi

# Authentik (rootless podman-compose, system service)
if systemctl list-unit-files | grep -q authentik.service; then
    echo "Enabling Authentik..."
    if systemctl is-active --quiet authentik; then
        systemctl restart authentik
    fi
    systemctl enable --now authentik.service
fi

# Miniflux
if systemctl list-unit-files | grep -q miniflux.service; then
    echo "Enabling Miniflux..."
    if systemctl is-active --quiet miniflux; then
        systemctl restart miniflux
    fi
    systemctl enable --now miniflux.service
fi
