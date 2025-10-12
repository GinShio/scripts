"""Compatibility wrapper exposing core templating utilities."""
from __future__ import annotations

from core.template import *  # noqa: F401,F403

__all__ = [
    "TemplateError",
    "TemplateResolver",
    "build_dependency_map",
    "extract_placeholders",
    "topological_order",
]
