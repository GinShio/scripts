"""Compatibility shim for accessing the builder package from a nested src layout."""
from __future__ import annotations

from importlib import import_module
from pathlib import Path
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

_src_root = Path(__file__).resolve().parent / "src" / __name__
if _src_root.is_dir():
    src_str = str(_src_root)
    if src_str not in __path__:
        __path__.append(src_str)

_cli_module = import_module(f"{__name__}.cli")
main = _cli_module.main

__all__ = ["main"]
