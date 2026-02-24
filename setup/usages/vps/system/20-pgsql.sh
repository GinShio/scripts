#!/bin/sh
#@tags: usage:vps, scope:system, os:debian, os:ubuntu
# System: Install PostgreSQL

set -e

echo "Installing PostgreSQL..."
export DEBIAN_FRONTEND=noninteractive
apt update -y
apt install -y postgresql postgresql-contrib

# Ensure PostgreSQL is running
systemctl enable --now postgresql

echo "PostgreSQL installation complete."
