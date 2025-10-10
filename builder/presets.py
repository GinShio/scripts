"""Preset resolution with inheritance, conditions, and templating."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping

from .template import TemplateResolver, TemplateError


@dataclass(slots=True)
class ResolvedPreset:
    environment: Dict[str, str] = field(default_factory=dict)
    definitions: Dict[str, Any] = field(default_factory=dict)
    extra_args: List[str] = field(default_factory=list)

    def merge(self, other: "ResolvedPreset") -> None:
        self.environment.update(other.environment)
        self.definitions.update(other.definitions)
        self.extra_args.extend(arg for arg in other.extra_args if arg not in self.extra_args)


class PresetRepository:
    def __init__(
        self,
        project_presets: Mapping[str, Mapping[str, Any]],
        shared_presets: Iterable[Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> None:
        self._presets: Dict[str, Mapping[str, Any]] = {
            key: value for key, value in project_presets.items()
        }
        if shared_presets:
            for preset_group in shared_presets:
                for key, value in preset_group.items():
                    if key not in self._presets:
                        self._presets[key] = value

    def available(self) -> Iterable[str]:
        return self._presets.keys()

    def resolve(
        self,
        preset_names: Iterable[str],
        *,
        template_resolver: TemplateResolver,
    ) -> ResolvedPreset:
        resolved = ResolvedPreset()
        for name in preset_names:
            name = name.strip()
            if not name:
                continue
            preset_resolution = self._resolve_single(
                name,
                template_resolver=template_resolver,
                seen=[],
            )
            if preset_resolution:
                resolved.merge(preset_resolution)
        return resolved

    def _resolve_single(
        self,
        name: str,
        *,
        template_resolver: TemplateResolver,
        seen: List[str],
    ) -> ResolvedPreset | None:
        if name in seen:
            raise TemplateError(f"Circular preset dependency detected: {' -> '.join(seen + [name])}")
        preset_data = self._presets.get(name)
        if preset_data is None:
            raise KeyError(f"Preset '{name}' not found. Available: {', '.join(sorted(self._presets))}")
        seen.append(name)

        extends: List[str] = []
        raw_extends = preset_data.get("extends")
        if isinstance(raw_extends, str):
            extends = [raw_extends]
        elif isinstance(raw_extends, Iterable):
            extends = [str(item) for item in raw_extends]

        resolved = ResolvedPreset()
        for parent_name in extends:
            parent_name = template_resolver.resolve(parent_name)
            parent = self._resolve_single(
                parent_name,
                template_resolver=template_resolver,
                seen=seen.copy(),
            )
            if parent:
                resolved.merge(parent)

        condition = preset_data.get("condition")
        if condition is not None:
            condition_value = template_resolver.resolve(condition)
            if not bool(condition_value):
                return resolved

        environment = preset_data.get("environment")
        if isinstance(environment, Mapping):
            env_values = {
                str(key): str(template_resolver.resolve(value))
                for key, value in environment.items()
            }
            resolved.environment.update(env_values)

        definitions = preset_data.get("definitions")
        if isinstance(definitions, Mapping):
            def_values: Dict[str, Any] = {}
            for key, value in definitions.items():
                def_values[str(key)] = template_resolver.resolve(value)
            resolved.definitions.update(def_values)

        extra_args = preset_data.get("extra_args")
        if isinstance(extra_args, Iterable) and not isinstance(extra_args, (str, bytes)):
            for value in extra_args:
                converted = template_resolver.resolve(value)
                resolved.extra_args.append(str(converted))

        return resolved
