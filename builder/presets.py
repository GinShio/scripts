"""Preset resolution with inheritance, conditions, and templating."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence

from .template import TemplateResolver, TemplateError, extract_placeholders, topological_order


@dataclass(slots=True)
class ResolvedPreset:
    environment: Dict[str, str] = field(default_factory=dict)
    definitions: Dict[str, Any] = field(default_factory=dict)
    extra_config_args: List[str] = field(default_factory=list)
    extra_build_args: List[str] = field(default_factory=list)

    def merge(self, other: "ResolvedPreset") -> None:
        self.environment.update(other.environment)
        self.definitions.update(other.definitions)
        self._extend_unique(self.extra_config_args, other.extra_config_args)
        self._extend_unique(self.extra_build_args, other.extra_build_args)

    @staticmethod
    def _extend_unique(target: List[str], values: Iterable[str]) -> None:
        existing = set(target)
        for value in values:
            if value not in existing:
                target.append(value)
                existing.add(value)


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

    @staticmethod
    def _augment_resolver(
        template_resolver: TemplateResolver,
        *,
        environment: Mapping[str, str],
        definitions: Mapping[str, Any],
    ) -> TemplateResolver:
        context: Dict[str, Any] = {
            key: value for key, value in template_resolver.context.items()
        }
        env_context = dict(context.get("env", {}))
        env_context.update(environment)
        context["env"] = env_context

        preset_context = dict(context.get("preset", {}))
        preset_context["environment"] = dict(environment)
        preset_context["definitions"] = dict(definitions)
        context["preset"] = preset_context
        return TemplateResolver(context)

    @staticmethod
    def _build_dependency_map(
        mapping: Mapping[str, Any],
        *,
        prefixes: Sequence[str],
        pre_resolved: Iterable[str] | None = None,
    ) -> Dict[str, List[str]]:
        dependency_map: Dict[str, List[str]] = {str(key): [] for key in mapping.keys()}
        keys_in_scope = set(dependency_map.keys())
        pre_resolved_keys = {str(key) for key in pre_resolved} if pre_resolved else set()
        for raw_key, value in mapping.items():
            key = str(raw_key)
            deps: set[str] = set()
            for placeholder in extract_placeholders(value):
                for prefix in prefixes:
                    if not placeholder.startswith(prefix):
                        continue
                    dep_token = placeholder[len(prefix):].strip()
                    if not dep_token:
                        continue
                    dep_name = dep_token.split(".", 1)[0]
                    if dep_name in pre_resolved_keys:
                        continue
                    if dep_name in keys_in_scope:
                        deps.add(dep_name)
            dependency_map[key] = sorted(deps)
        return dependency_map

    def _resolve_environment_map(
        self,
        raw_environment: Mapping[str, Any],
        *,
        template_resolver: TemplateResolver,
        base_environment: Mapping[str, str],
        definitions: Mapping[str, Any],
    ) -> Dict[str, str]:
        if not raw_environment:
            return {}

        normalized_environment: Dict[str, Any] = {str(key): value for key, value in raw_environment.items()}

        resolved: Dict[str, str] = {}
        base_env: Dict[str, str] = {}
        base_env.update(template_resolver.context.get("env", {}))
        base_env.update(base_environment)

        dependency_map = self._build_dependency_map(
            normalized_environment,
            prefixes=("env.", "preset.environment."),
            pre_resolved=base_env.keys(),
        )
        order = topological_order(dependency_map)

        for key in order:
            value = normalized_environment[key]
            current_env = {**base_env, **resolved}
            augmented_resolver = self._augment_resolver(
                template_resolver,
                environment=current_env,
                definitions=definitions,
            )
            result = augmented_resolver.resolve(value)
            resolved[key] = str(result)
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
            env_values = self._resolve_environment_map(
                environment,
                template_resolver=template_resolver,
                base_environment=resolved.environment,
                definitions=resolved.definitions,
            )
            resolved.environment.update(env_values)

        definitions = preset_data.get("definitions")
        if isinstance(definitions, Mapping):
            normalized_definitions: Dict[str, Any] = {str(key): value for key, value in definitions.items()}
            dependency_map = self._build_dependency_map(
                normalized_definitions,
                prefixes=("preset.definitions.",),
                pre_resolved=resolved.definitions.keys(),
            )
            order = topological_order(dependency_map)
            def_values: Dict[str, Any] = {}
            for key in order:
                current_definitions = {**resolved.definitions, **def_values}
                augmented_resolver = self._augment_resolver(
                    template_resolver,
                    environment=resolved.environment,
                    definitions=current_definitions,
                )
                def_values[str(key)] = augmented_resolver.resolve(normalized_definitions[key])
            resolved.definitions.update(def_values)

        def _collect_args(raw_value: Any) -> List[str]:
            collected: List[str] = []
            if isinstance(raw_value, Iterable) and not isinstance(raw_value, (str, bytes)):
                for value in raw_value:
                    converted = template_resolver.resolve(value)
                    collected.append(str(converted))
            elif isinstance(raw_value, (str, bytes)):
                converted = template_resolver.resolve(raw_value)
                collected.append(str(converted))
            return collected

        config_args = _collect_args(preset_data.get("extra_config_args"))
        if config_args:
            ResolvedPreset._extend_unique(resolved.extra_config_args, config_args)

        build_args = _collect_args(preset_data.get("extra_build_args"))
        if build_args:
            ResolvedPreset._extend_unique(resolved.extra_build_args, build_args)

        return resolved
