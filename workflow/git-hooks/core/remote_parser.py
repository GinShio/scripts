#!/usr/bin/env python3
import re
import subprocess
import sys


def resolve_ssh_alias(host):
    """Resolve SSH alias using 'ssh -G'."""
    try:
        # ssh -G <host> prints config. We look for 'hostname' key.
        proc = subprocess.run(['ssh', '-G', host], capture_output=True, text=True, timeout=2)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if line.lower().startswith('hostname '):
                    return line.split(' ', 1)[1].strip()
    except Exception:
        pass
    return host

def normalize_domain(domain):
    """Normalize domain for specific services (e.g. ssh.github.com -> github.com)."""
    domain = domain.lower()
    if domain == 'ssh.github.com':
        return 'github.com'
    if domain == 'altssh.gitlab.com':
        return 'gitlab.com'
    if domain == 'ssh.dev.azure.com':
        return 'dev.azure.com'
    if domain == 'vs-ssh.visualstudio.com':
        return 'visualstudio.com'
    if domain == 'altssh.bitbucket.org':
        return 'bitbucket.org'
    # Handle subdomains maybe? for now explicit mapping is safer.
    return domain

def parse_url(url):
    domain = ''
    path = ''

    # 1. Check for standard SCP-like SSH syntax: user@host:path/to/repo.git
    # also handles simple alias: alias:repo/path
    sc_match = re.match(r'^(?:[^@]+@)?([^:]+):(.+)$', url)

    # Note: SCP syntax does not start with protocol://
    if sc_match and not any(url.startswith(p) for p in ['http:', 'https:', 'ssh:', 'git:']):
        raw_host = sc_match.group(1)
        path = sc_match.group(2)
        resolved_host = resolve_ssh_alias(raw_host)
        domain = normalize_domain(resolved_host)
    else:
        # 2. Generic URI matching (https, ssh://, git://)
        # Regex: protocol://[user@]host[:port]/path
        match = re.search(r'^(?:ssh|git|https?)://(?:[^@/]+@)?([^:/]+)(?::\d+)?/(.+)$', url)
        if match:
            raw_host = match.group(1)
            path = match.group(2)
            resolved_host = resolve_ssh_alias(raw_host)
            domain = normalize_domain(resolved_host)

    if domain and path:
        # Cleanup path (remove .git, remove leading /)
        if path.endswith('.git'):
            path = path[:-4]
        path = path.strip('/')
        
        # Split org/repo
        parts = path.split('/')
        if len(parts) >= 2:
            # Take last two parts usually: org/repo
            # but for deeply nested like gitlab groups: group/subgroup/repo
            # machete usually wants org = group/subgroup, repo = repo
            
            repo = parts[-1]
            org = '/'.join(parts[:-1])
            
            print(f"{domain} {org} {repo}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        parse_url(sys.argv[1])
