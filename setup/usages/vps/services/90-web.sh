#!/bin/sh
#@tags: usage:vps, scope:services, os:debian, os:ubuntu
# Services: Enable and start VPS web services

set -e

# Nginx
if systemctl list-unit-files | grep -q nginx.service; then
    echo "Enabling Nginx..."
    systemctl enable --now nginx.service
fi
