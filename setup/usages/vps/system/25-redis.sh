#!/bin/sh
#@tags: usage:vps, scope:system, os:debian, os:ubuntu
# System: Install Redis/Valkey

set -e

echo "Installing Redis..."
export DEBIAN_FRONTEND=noninteractive
apt update -y
apt install -y redis-server

# Configure Redis to use ACLs and requirepass for default user (optional but recommended)
# We will leave the default user without password for local socket/localhost if preferred,
# or we can just rely on ACLs for specific apps.
# By default, Redis binds to 127.0.0.1 and ::1.

# Enable ACL file
if ! grep -q "^aclfile /etc/redis/users.acl" /etc/redis/redis.conf; then
    echo "aclfile /etc/redis/users.acl" >> /etc/redis/redis.conf
    touch /etc/redis/users.acl
    chown redis:redis /etc/redis/users.acl
    chmod 600 /etc/redis/users.acl
fi

systemctl enable --now redis-server
systemctl restart redis-server

echo "Redis installation complete."
