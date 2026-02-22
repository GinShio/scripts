#!/bin/sh
#@tags: usage:vps, scope:system, os:debian, os:ubuntu
# System: Configure Login, SSH and Fail2ban for VPS

set -e

echo "Configuring Login, SSH and Fail2ban..."

LOGIN_USERNAME=${LOGIN_USERNAME:-ginshio}
SSH_PORT=${SSH_PORT:-2222}

# 1. Configure User
if ! id "$LOGIN_USERNAME" >/dev/null 2>&1; then
    echo "Creating user $LOGIN_USERNAME..."
    useradd "$LOGIN_USERNAME" --create-home --shell /usr/bin/dash --password "$(cat /proc/sys/kernel/random/uuid | openssl passwd -6 -stdin)"
fi

# Ensure user is unlocked
passwd -u "$LOGIN_USERNAME" >/dev/null 2>&1 || true

# Sudoers
echo "$LOGIN_USERNAME ALL=NOPASSWD: ALL" | (EDITOR="tee -a" visudo)

# SSH Keys
if [ -n "${LOGIN_PUBKEY:-}" ]; then
    echo "Setting up SSH keys for $LOGIN_USERNAME..."
    sudo -u "$LOGIN_USERNAME" mkdir -p "/home/$LOGIN_USERNAME/.ssh"
    echo "$LOGIN_PUBKEY" | sudo -u "$LOGIN_USERNAME" tee "/home/$LOGIN_USERNAME/.ssh/authorized_keys" >/dev/null
    chmod 600 "/home/$LOGIN_USERNAME/.ssh/authorized_keys"
fi

# 2. Configure SSH
echo "Configuring SSHd..."
if [ ! -f /etc/ssh/sshd_config.bak ]; then
    cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak
fi

if [ -n "${LOGIN_PUBKEY:-}" ]; then
    echo "LOGIN_PUBKEY is set. Enforcing strict SSH configuration..."
    cat <<-EOF > /etc/ssh/sshd_config
Port $SSH_PORT
Protocol 2

MaxAuthTries 8
MaxSessions 32

RSAAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
PasswordAuthentication no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
EOF
else
    echo "LOGIN_PUBKEY is NOT set. Applying basic SSH configuration..."
    cat <<-EOF > /etc/ssh/sshd_config
Port $SSH_PORT
Protocol 2

MaxAuthTries 8
MaxSessions 32

PubkeyAuthentication yes
PermitRootLogin no
# PasswordAuthentication yes
EOF
fi

# 3. Configure Fail2ban
echo "Configuring Fail2ban..."
if [ -f /etc/fail2ban/fail2ban.conf ]; then
    sed -i 's/loglevel = INFO/loglevel = CRITICAL/' /etc/fail2ban/fail2ban.conf
fi

if [ -f /etc/fail2ban/jail.conf ]; then
    sed -i -E "s/maxretry[[:blank:]]*=[[:blank:]]*5/maxretry=3/; s/bantime[[:blank:]]+=.*/bantime=1w/g; s/port[[:blank:]]*=[[:blank:]]*ssh/port=$SSH_PORT/g" /etc/fail2ban/jail.conf
fi

echo "Login configuration completed."
