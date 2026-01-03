"""Builder CLI package implementing configuration-driven build orchestration."""

from core.template import TemplateError, TemplateResolver
from .cli import main

__all__ = ["main", "TemplateError", "TemplateResolver"]
