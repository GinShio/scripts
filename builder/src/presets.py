"""Preset resolution with inheritance, conditions, and templating."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence

from core.template import TemplateResolver, TemplateError, build_dependency_map, topological_order


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

    def clone(self) -> "ResolvedPreset":
        return ResolvedPreset(
            environment=dict(self.environment),
            definitions=dict(self.definitions),
            extra_config_args=list(self.extra_config_args),
            extra_build_args=list(self.extra_build_args),
        )


@dataclass(slots=True)
class PresetDefinition:
    extends: tuple[str, ...]
    condition: Any | None
    environment: Dict[str, Any]
    environment_dependencies: Dict[str, List[str]]
    definitions: Dict[str, Any]
    definition_dependencies: Dict[str, List[str]]
    extra_config_args: Sequence[Any]
    extra_build_args: Sequence[Any]


class PresetRepository:
    def __init__(
        self,
        project_presets: Mapping[str, Mapping[str, Any]],
        shared_presets: Iterable[Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> None:
        self._presets: Dict[str, PresetDefinition] = {}
        for key, value in project_presets.items():
            self._presets[key] = self._normalize_definition(value)
        if shared_presets:
            for preset_group in shared_presets:
                for key, value in preset_group.items():
                    if key not in self._presets:
                        self._presets[key] = self._normalize_definition(value)

    def available(self) -> Iterable[str]:
        return self._presets.keys()

    def resolve(
        self,
        preset_names: Iterable[str],
        *,
        template_resolver: TemplateResolver,
    ) -> ResolvedPreset:
        resolved = ResolvedPreset()
        cache: Dict[str, ResolvedPreset] = {}
        for name in preset_names:
            name = name.strip()
            if not name:
                continue
            preset_resolution = self._resolve_single(
                name,
                template_resolver=template_resolver,
                seen=(),
                cache=cache,
            )
            if preset_resolution:
                resolved.merge(preset_resolution)
        return resolved

    @staticmethod
    def _normalize_definition(raw_preset: Mapping[str, Any]) -> PresetDefinition:
        extends: tuple[str, ...] = ()
        raw_extends = raw_preset.get("extends")
        if isinstance(raw_extends, str):
            extends = (raw_extends.strip(),)
        elif isinstance(raw_extends, Iterable) and not isinstance(raw_extends, (bytes, str)):
            extends = tuple(str(item).strip() for item in raw_extends if str(item).strip())

        environment: Dict[str, Any] = {}
        environment_dependencies: Dict[str, List[str]] = {}
        raw_environment = raw_preset.get("environment")
        if isinstance(raw_environment, Mapping):
            environment = {str(key): value for key, value in raw_environment.items()}
            environment_dependencies = build_dependency_map(
                environment,
                prefixes=("env.", "preset.environment."),
                pre_resolved=(),
            )

        definitions: Dict[str, Any] = {}
        definition_dependencies: Dict[str, List[str]] = {}
        raw_definitions = raw_preset.get("definitions")
        if isinstance(raw_definitions, Mapping):
            definitions = {str(key): value for key, value in raw_definitions.items()}
            definition_dependencies = build_dependency_map(
                definitions,
                prefixes=("preset.definitions.",),
                pre_resolved=(),
            )

        def _normalize_args(raw_value: Any) -> tuple[Any, ...]:
            if isinstance(raw_value, Iterable) and not isinstance(raw_value, (str, bytes)):
                return tuple(raw_value)
            if raw_value is None:
                return ()
            return (raw_value,)

        extra_config_args = _normalize_args(raw_preset.get("extra_config_args"))
        extra_build_args = _normalize_args(raw_preset.get("extra_build_args"))

        condition = raw_preset.get("condition")

        return PresetDefinition(
            extends=extends,
            condition=condition,
            environment=environment,
            environment_dependencies=environment_dependencies,
            definitions=definitions,
            definition_dependencies=definition_dependencies,
            extra_config_args=extra_config_args,
            extra_build_args=extra_build_args,
        )

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

    def _resolve_environment_map(
        self,
        raw_environment: Mapping[str, Any],
        *,
        template_resolver: TemplateResolver,
        base_environment: Mapping[str, str],
        definitions: Mapping[str, Any],
        dependency_map: Mapping[str, Sequence[str]],
    ) -> Dict[str, str]:
        if not raw_environment:
            return {}

        normalized_environment: Dict[str, Any] = {str(key): value for key, value in raw_environment.items()}

        resolved: Dict[str, str] = {}
        base_env: Dict[str, Any] = {
            str(key): value for key, value in template_resolver.context.get("env", {}).items()
        }
        base_env.update({str(key): value for key, value in base_environment.items()})

        context_template: Dict[str, Any] = dict(template_resolver.context)
        env_context: Dict[str, Any] = dict(context_template.get("env", {}))
        context_template["env"] = env_context

        preset_context: Dict[str, Any] = dict(context_template.get("preset", {}))
        preset_environment: Dict[str, Any] = dict(base_environment)
        preset_context["environment"] = preset_environment
        preset_context["definitions"] = dict(definitions)
        context_template["preset"] = preset_context

        reusable_resolver = TemplateResolver(context_template)

        filtered_dependencies: Dict[str, List[str]] = {
            key: [dep for dep in dependency_map.get(key, []) if dep not in base_env]
            for key in normalized_environment.keys()
        }
        order = topological_order(filtered_dependencies)

        for key in order:
            value = normalized_environment[key]
            env_context.clear()
            env_context.update(base_env)
            env_context.update(resolved)

            preset_environment.clear()
            preset_environment.update(base_environment)
            preset_environment.update(resolved)

            reusable_resolver.clear_cache()
            result = reusable_resolver.resolve(value)
            resolved[key] = str(result)
        return resolved

    def _resolve_single(
        self,
        name: str,
        *,
        template_resolver: TemplateResolver,
        seen: tuple[str, ...],
        cache: MutableMapping[str, ResolvedPreset],
    ) -> ResolvedPreset | None:
        if name in seen:
            raise TemplateError(f"Circular preset dependency detected: {' -> '.join(seen + (name,))}")
        if name in cache:
            return cache[name].clone()

        preset_data = self._presets.get(name)
        if preset_data is None:
            raise KeyError(f"Preset '{name}' not found. Available: {', '.join(sorted(self._presets))}")
        next_seen = seen + (name,)

        resolved = ResolvedPreset()
        for parent_name in preset_data.extends:
            parent_key = str(template_resolver.resolve(parent_name)).strip()
            if not parent_key:
                continue
            parent = self._resolve_single(
                parent_key,
                template_resolver=template_resolver,
                seen=next_seen,
                cache=cache,
            )
            if parent:
                resolved.merge(parent)

        condition = preset_data.condition
        if condition is not None:
            condition_value = template_resolver.resolve(condition)
            if not bool(condition_value):
                cache[name] = resolved.clone()
                return resolved

        environment = preset_data.environment
        if environment:
            env_values = self._resolve_environment_map(
                environment,
                template_resolver=template_resolver,
                base_environment=resolved.environment,
                definitions=resolved.definitions,
                dependency_map=preset_data.environment_dependencies,
            )
            resolved.environment.update(env_values)

        definitions = preset_data.definitions
        if definitions:
            normalized_definitions: Dict[str, Any] = dict(definitions)
            filtered_dependencies: Dict[str, List[str]] = {
                key: [dep for dep in preset_data.definition_dependencies.get(key, []) if dep not in resolved.definitions]
                for key in normalized_definitions.keys()
            }
            order = topological_order(filtered_dependencies)
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

        def _collect_args(raw_values: Sequence[Any]) -> List[str]:
            collected: List[str] = []
            for value in raw_values:
                converted = template_resolver.resolve(value)
                collected.append(str(converted))
            return collected

        config_args = _collect_args(preset_data.extra_config_args)
        if config_args:
            ResolvedPreset._extend_unique(resolved.extra_config_args, config_args)

        build_args = _collect_args(preset_data.extra_build_args)
        if build_args:
            ResolvedPreset._extend_unique(resolved.extra_build_args, build_args)

        cache[name] = resolved.clone()
        return resolved
