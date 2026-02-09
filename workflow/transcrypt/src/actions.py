from __future__ import annotations

import base64
import getpass
import os

#
# transcrypt - https://github.com/elasticdog/transcrypt
#
# A script to configure transparent encryption of sensitive files stored in
# a Git repository. It utilizes OpenSSL's symmetric cipher routines and follows
# the gitattributes(5) man page regarding the use of filters.
#
# Copyright (c) 2019-2025 James Murty <james@murty.co>
# Copyright (c) 2014-2020 Aaron Bull Schaefer <aaron@elasticdog.com>
#
# Add PR https://github.com/elasticdog/transcrypt/pull/132 for salt and pbkdf2
# Copyright (c) 2022-2025 Jon Crall <jon.crall@kitware.com>
#
# This source code is provided under the terms of the MIT License
# that can be be found in the LICENSE file.
#
# --------------------------------------------------------------------------------
# Ported to Python by GinShio
# Copyright (c) 2026 GinShio
# --------------------------------------------------------------------------------
#
import sys
from pathlib import Path
from typing import Optional

from core import crypto

from .utils import (
    get_git_config,
    get_git_dir,
    get_git_root,
    get_relative_path,
    set_git_config,
    unset_git_config,
)

TRANSCRYPT_PREFIX = "transcrypt"


def _get_env_val(key: str, context: str = "default") -> Optional[str]:
    """
    Get value from environment variable.
    Naming convention:
      1. TRANSCRYPT_<CONTEXT>_<KEY> (e.g. TRANSCRYPT_DEFAULT_PASSWORD, TRANSCRYPT_PROD_PASSWORD)
      2. TRANSCRYPT_<KEY> (Legacy/Short for default context only)
    """
    env_key = key.upper()
    context_upper = context.upper()

    # 1. Try Specific context key: TRANSCRYPT_DEFAULT_PASSWORD or TRANSCRYPT_CTX_PASSWORD
    val = os.getenv(f"TRANSCRYPT_{context_upper}_{env_key}")
    if val:
        return val

    # 2. Try Short key if default: TRANSCRYPT_PASSWORD
    if context == "default":
        return os.getenv(f"TRANSCRYPT_{env_key}")

    return None


def _get_config_key(key: str, context: str = "default") -> str:
    if context == "default":
        return f"{TRANSCRYPT_PREFIX}.{key}"
    return f"{TRANSCRYPT_PREFIX}.{context}.{key}"


def _get_password(context: str = "default") -> str:
    # 1. Try Environment Variable
    pwd = _get_env_val("password", context)
    if pwd:
        return pwd

    # 2. Try Git Config
    key = _get_config_key("password", context)
    pwd = get_git_config(key)
    if not pwd:
        # If not in config, maybe env var?
        # Original transcrypt checks config.
        # If running as filter, we can't prompt.
        raise ValueError(
            f"Password not found in git config for context '{context}' (or env var). Run 'configure' first."
        )
    return pwd


def _get_cipher(context: str = "default") -> str:
    # 1. Try Environment Variable
    val = _get_env_val("cipher", context)
    if val:
        return val

    # 2. Try Git Config
    key = _get_config_key("cipher", context)
    cipher = get_git_config(key)
    return cipher or crypto.DEFAULT_CIPHER


def _get_digest(context: str = "default") -> str:
    # 1. Try Environment Variable
    val = _get_env_val("digest", context)
    if val:
        return val

    # 2. Try Git Config
    key = _get_config_key("digest", context)
    digest = get_git_config(key)
    return digest or crypto.DEFAULT_DIGEST


def _get_iterations(context: str = "default") -> int:
    # 1. Try Environment Variable
    val = _get_env_val("iterations", context)
    if val:
        try:
            return int(val)
        except ValueError:
            pass

    # 2. Try Git Config
    key = _get_config_key("iterations", context)
    val = get_git_config(key)
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return crypto.DEFAULT_ITERATIONS


def _get_kdf(context: str = "default") -> str:
    # 1. Try Environment Variable
    val = _get_env_val("kdf", context)
    if val:
        return val

    # 2. Try Git Config
    key = _get_config_key("kdf", context)
    kdf = get_git_config(key)
    return kdf or crypto.DEFAULT_KDF


# deterministic option is removed, enforced by design


def clean(context: str = "default", file_path: Optional[str] = None):
    """
    Encrypt data from stdin to stdout.
    Used by git filter clean.
    """
    try:
        # Read all bytes from stdin
        # Note: git passes the file content to stdin
        data = sys.stdin.buffer.read()

        # If empty, just return empty?
        if not data:
            return

        password = _get_password(context)
        cipher = _get_cipher(context)
        digest = _get_digest(context)
        iterations = _get_iterations(context)
        kdf = _get_kdf(context)

        # Prepare context for SIV
        siv_context = b""
        if file_path:
            # Normalize path to ensure consistency (Unix style)
            siv_context = Path(file_path).as_posix().encode("utf-8")

        # Encrypt
        # core.crypto.encrypt returns base64 bytes
        encrypted_b64 = crypto.encrypt(
            data,
            password,
            cipher_name=cipher,
            digest=digest,
            iterations=iterations,
            deterministic=True,  # Transcrypt script always uses deterministic mode
            context=siv_context,
            kdf=kdf,
        )

        # Write to stdout
        sys.stdout.buffer.write(encrypted_b64)
    except Exception as e:
        # Git filters should fail gracefully or loud?
        # Usually loud is better so the user knows something went wrong.
        print(f"Encryption failed: {e}", file=sys.stderr)
        sys.exit(1)


def smudge(context: str = "default", file_path: Optional[str] = None):
    """
    Decrypt data from stdin to stdout.
    Used by git filter smudge.
    """
    try:
        # Read all bytes from stdin
        data = sys.stdin.buffer.read()

        # Check if empty
        if not data:
            return

        # Check if it looks like our encrypted format
        # It should be base64 and start with Salted__ header when decoded,
        # but here we work on bytes.
        # Optimization: Check if it looks like base64 or has expected headers.
        # But core.crypto.decrypt handles this checks.

        # Quick check: if the file is NOT encrypted (e.g. was checked out without filter before),
        # we shouldn't fail, we should just pass it through?
        # Transcrypt script behavior:
        # "decrypts the file if it is encrypted, otherwise cat the file"

        # Try to basic validate
        try:
            # We can't easily check validity without decoding base64
            # and checking Salted__ header.
            # But the overhead is low.
            decoded_chk = base64.b64decode(data, validate=True)
            if not decoded_chk.startswith(crypto.SALT_HEADER):
                # Not encrypted by us (or at least not with Salted__ header)
                # Pass through
                sys.stdout.buffer.write(data)
                return
        except Exception:
            # Not valid base64 -> not encrypted
            sys.stdout.buffer.write(data)
            return

        try:
            password = _get_password(context)
        except ValueError:
            # Password not found. Graceful degradation: output raw encrypted data.
            # This allows checkout of repositories with multiple transcrypt contexts
            # even if not all passwords are present (files remain encrypted).
            sys.stdout.buffer.write(data)
            return

        cipher = _get_cipher(context)
        digest = _get_digest(context)
        iterations = _get_iterations(context)
        kdf = _get_kdf(context)

        # Prepare context for SIV
        siv_context = b""
        if file_path:
            siv_context = Path(file_path).as_posix().encode("utf-8")

        # Decrypt
        decrypted = crypto.decrypt(
            data,
            password,
            cipher_name=cipher,
            digest=digest,
            iterations=iterations,
            deterministic=True,  # Transcrypt script always uses deterministic checks
            context=siv_context,
            kdf=kdf,
        )

        sys.stdout.buffer.write(decrypted)

    except Exception as e:
        # Check if user explicitly allows fallback
        allow_fallback = os.environ.get("TRANSCRYPT_ALLOW_RAW_FALLBACK", "")

        if allow_fallback == "1" or allow_fallback.lower() == "true":
            # Fallback Mode: Output raw data
            print(
                f"Warning: Decryption failed ({e}). Outputting raw data (Fallback Mode).",
                file=sys.stderr,
            )
            # Ensure we write the original data
            sys.stdout.buffer.write(data)
            sys.exit(0)
        else:
            # If decryption fails for other reasons (authentication error usually),
            # we might want to fail or fallback.
            # Failing hard is safer to warn user that "Hey, you have a password but it's WRONG".
            # Missing password is one thing (checkout encrypted), wrong password is another.
            print(f"Decryption failed: {e}", file=sys.stderr)
            sys.exit(1)


def configure(
    password: Optional[str] = None,
    cipher: Optional[str] = None,
    context: str = "default",
):
    """
    Deprecated: Use git config directly.
    """
    print(
        "Command 'configure' is deprecated/disabled. Please use 'git config' directly.",
        file=sys.stderr,
    )
    print("Example:", file=sys.stderr)
    print(f"  git config transcrypt.password <your_password>", file=sys.stderr)
    sys.exit(1)


def install(context: str = "default"):
    """
    Install git filters.
    """
    # git config filter.transcrypt.clean 'python3 ... clean'
    # git config filter.transcrypt.smudge 'python3 ... smudge'
    # We need to construct the command line to invoke ourselves.

    # We are running from a script. We need to find the entry point.
    # Assuming this script is run via `python3 workflow/transcrypt.py`
    # Or installed somewhere.

    # Best effort: Use sys.executable and the path to workflow/transcrypt.py
    # We use path relative to git root if possible, to support relocatable repos.

    wrapper_path = Path(sys.argv[0]).resolve()
    # Try to make it relative to git root
    try:
        wrapper_path = get_relative_path(wrapper_path)
    except Exception:
        # Keep absolute if failure or outside
        pass

    wrapper_str = str(wrapper_path)
    if os.sep in wrapper_str:
        # If it has path separators, it might need to be run from root.
        pass

    driver_name = "transcrypt"
    if context != "default":
        driver_name = f"transcrypt-{context}"

    cmd_base = f"{sys.executable} {wrapper_str}"

    # We pass %f as argument to clean/smudge
    clean_cmd = f"{cmd_base} -c {context} clean %f"
    smudge_cmd = f"{cmd_base} -c {context} smudge %f"

    set_git_config(f"filter.{driver_name}.clean", clean_cmd)
    set_git_config(f"filter.{driver_name}.smudge", smudge_cmd)
    set_git_config(f"filter.{driver_name}.required", "true")

    # Also diff driver?
    # git config diff.transcrypt.textconv '...'
    textconv_cmd = f"{cmd_base} -c {context} textconv"
    set_git_config(f"diff.{driver_name}.textconv", textconv_cmd)

    print(f"Git filters for '{driver_name}' configured.")
    print(f"To enforce encryption, add the following to your .gitattributes file:")
    print(f"  <pattern> filter={driver_name} diff={driver_name} merge={driver_name}")


def uninstall(context: str = "default"):
    driver_name = "transcrypt"
    if context != "default":
        driver_name = f"transcrypt-{context}"

    unset_git_config(f"filter.{driver_name}.clean")
    unset_git_config(f"filter.{driver_name}.smudge")
    unset_git_config(f"filter.{driver_name}.required")
    unset_git_config(f"diff.{driver_name}.textconv")

    print(f"Git filters for '{driver_name}' removed.")


def textconv(file_path: str, context: str = "default"):
    """
    Decryption for textconv (diff).
    Takes a filename as argument, decrypts it to stdout.
    Note: git textconv passes the filename.
    """
    try:
        with open(file_path, "rb") as f:
            data = f.read()

        # Try to decrypt
        try:
            decoded_chk = base64.b64decode(data, validate=True)
            if not decoded_chk.startswith(crypto.SALT_HEADER):
                sys.stdout.buffer.write(data)
                return
        except Exception:
            sys.stdout.buffer.write(data)
            return

        try:
            password = _get_password(context)
        except ValueError:
            # Graceful degradation for diff as well
            sys.stdout.buffer.write(data)
            return

        cipher = _get_cipher(context)
        digest = _get_digest(context)
        iterations = _get_iterations(context)

        # Textconv context
        siv_context = b""
        if file_path:
            siv_context = Path(file_path).as_posix().encode("utf-8")

        decrypted = crypto.decrypt(
            data,
            password,
            cipher_name=cipher,
            digest=digest,
            iterations=iterations,
            deterministic=True,
            context=siv_context,
        )
        sys.stdout.buffer.write(decrypted)

    except Exception as e:
        # Check if user explicitly allows fallback
        allow_fallback = os.environ.get("TRANSCRYPT_ALLOW_RAW_FALLBACK", "")

        if allow_fallback == "1" or allow_fallback.lower() == "true":
            # Fallback Mode: Output raw data
            print(
                f"Warning: Textconv decryption failed ({e}). Outputting raw data (Fallback Mode).",
                file=sys.stderr,
            )
            # Ensure we write the original data
            sys.stdout.buffer.write(data)
            sys.exit(0)
        else:
            print(f"Textconv failed: {e}", file=sys.stderr)
            sys.exit(1)


def status(context: str = "default"):
    """
    Check configuration status.
    """
    driver_name = "transcrypt"
    if context != "default":
        driver_name = f"transcrypt-{context}"

    # Check Env Vars
    env_pwd = _get_env_val("password", context)
    env_cipher = _get_env_val("cipher", context)
    env_digest = _get_env_val("digest", context)
    env_iterations = _get_env_val("iterations", context)

    # Check Git Config
    git_pwd = get_git_config(_get_config_key("password", context))
    git_cipher = get_git_config(_get_config_key("cipher", context))
    git_digest = get_git_config(_get_config_key("digest", context))
    git_iterations = get_git_config(_get_config_key("iterations", context))

    # Determine effective values
    pwd = env_pwd or git_pwd
    pwd_source = "Env Var" if env_pwd else ("Git Config" if git_pwd else "NOT SET")
    masked_pwd = "****" if pwd else "NOT SET"

    cipher = env_cipher or git_cipher
    cipher_source = (
        "Env Var" if env_cipher else ("Git Config" if git_cipher else "Default")
    )
    cipher = cipher or crypto.DEFAULT_CIPHER

    digest = env_digest or git_digest
    digest_source = (
        "Env Var" if env_digest else ("Git Config" if git_digest else "Default")
    )
    digest = digest or crypto.DEFAULT_DIGEST

    iterations = env_iterations or git_iterations
    iterations_source = (
        "Env Var" if env_iterations else ("Git Config" if git_iterations else "Default")
    )
    iterations = iterations or crypto.DEFAULT_ITERATIONS

    clean_filter = get_git_config(f"filter.{driver_name}.clean")
    is_installed = clean_filter is not None

    print(f"Status for context '{context}':")
    print(f"  Password:   {masked_pwd} ({pwd_source})")
    print(f"  Cipher:     {cipher} ({cipher_source})")
    print(f"  Digest:     {digest} ({digest_source})")
    print(f"  Iterations: {iterations} ({iterations_source})")
    print(f"  Deterministic: enforced")
    print(f"  Filters:    {'Installed' if is_installed else 'Not Installed'}")

    if is_installed:
        print(f"  Clean CMD: {clean_filter}")

    if not pwd:
        print(
            "\nWarning: Password not found in git config or environment. Encryption/Decryption will fail."
        )
        print(
            f"Run 'git config transcrypt{'.' + context if context != 'default' else ''}.password <your-password>'"
        )
        print(f"OR set TRANSCRYPT_{context.upper()}_PASSWORD environment variable.")

    if not is_installed:
        print(
            "\nWarning: Git filters not installed. Automatic encryption will not work."
        )
        print(f"Run 'workflow/transcrypt.py install -c {context}' to register filters.")
