"""Builder CLI package entry point with dynamic source discovery."""
from __future__ import annotations

from importlib import import_module
from pathlib import Path
import sys

_pkg_root = Path(__file__).resolve().parent
_src_root = _pkg_root / "src"

if _src_root.is_dir():
	src_path = str(_src_root)
	if src_path not in sys.path:
		sys.path.append(src_path)
	if src_path not in __path__:
		__path__.append(src_path)

from core.template import TemplateError, TemplateResolver  # noqa: E402

_cli_module = import_module("builder.cli")
main = _cli_module.main

__all__ = ["main", "TemplateError", "TemplateResolver"]
