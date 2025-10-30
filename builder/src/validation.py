"""Configuration and template validation helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import re

from core.template import (
    TemplateError,
    build_dependency_map,
    topological_order,
    validate_expression_syntax,
    validate_variables,
)

from .config_loader import ConfigurationStore, ProjectDefinition
from .environment import ContextBuilder


_EXPRESSION_PATTERN = re.compile(r"^\s*\[\[(?P<expr>.*)\]\]\s*$", re.S)
_PLACEHOLDER_PATTERN = re.compile(r"\{\{[^{}]+\}\}")


def validate_store_structure(store: ConfigurationStore) -> list[str]:
    """Validate cross-project configuration such as shared preset definitions."""

    errors: list[str] = []
    all_preset_names = _collect_all_preset_names(store)
    errors.extend(store.toolchains.validate())
    for key, shared_config in store.shared_configs.items():
        presets_section = shared_config.get("presets")
        if isinstance(presets_section, Mapping):
            _validate_preset_collection(
                presets_section,
                available_presets=all_preset_names,
                errors=errors,
                origin=f"shared config '{key}'",
            )
    return errors


def _ensure_no_cycles(mapping: Mapping[str, Any], *, prefixes: Sequence[str], label: str) -> None:
    if not mapping:
        return

    normalized: dict[str, Any] = {str(key): value for key, value in mapping.items()}
    dependency_map = build_dependency_map(normalized, prefixes=prefixes)
    try:
        topological_order(dependency_map)
    except TemplateError as exc:
        raise TemplateError(f"{label} circular dependency: {exc}") from exc


def validate_project(
    store: ConfigurationStore,
    name: str,
    *,
    workspace: Path,
) -> None:
    """Validate a single project configuration and template usage."""

    project = store.get_project(name)

    errors: list[str] = []
    errors.extend(project.validate_structure())
    errors.extend(_validate_project_presets(project, store))

    if errors:
        raise ValueError("; ".join(errors))


def validate_project_templates(
    store: ConfigurationStore,
    name: str,
    *,
    workspace: Path,
) -> None:
    """Validate template placeholders for a project without planning builds."""

    project = store.get_project(name)

    context_builder = ContextBuilder(workspace)
    system_ctx = context_builder.system()

    branch = project.git.component_branch or project.git.main_branch
    default_build_type = store.global_config.default_build_type
    operation = store.global_config.default_operation

    user_ctx = context_builder.user(
        branch=branch,
        build_type=default_build_type,
        generator=project.generator,
        operation=operation,
        toolchain=None,
        linker=None,
        cc=None,
        cxx=None,
        launcher=None,
    )

    project_ctx = context_builder.project(
        name=project.name,
        source_dir=Path(project.source_dir),
        build_dir=Path(project.build_dir) if project.build_dir else None,
        install_dir=Path(project.install_dir) if project.install_dir else None,
        component_dir=Path(project.component_dir) if project.component_dir else None,
        environment=project.environment,
        org=project.org,
    )

    combined_context = context_builder.combined_context(user=user_ctx, project=project_ctx, system=system_ctx)
    combined_context.setdefault("preset", {"environment": {}, "definitions": {}})

    user_mapping = dict(combined_context.get("user", {}))
    for key in ("toolchain", "linker", "cc", "cxx"):
        user_mapping.setdefault(key, "")
    combined_context["user"] = user_mapping

    project_templates: dict[str, Any] = {
        "source_dir": project.source_dir,
        "build_dir": project.build_dir,
        "install_dir": project.install_dir,
        "component_dir": project.component_dir,
        "environment": project.environment,
        "extra_config_args": project.extra_config_args,
        "extra_build_args": project.extra_build_args,
    }

    if isinstance(project.environment, Mapping):
        _ensure_no_cycles(
            project.environment,
            prefixes=("project.environment.",),
            label=f"Project '{project.name}' environment",
        )

    validate_variables(
        context=combined_context,
        values={key: value for key, value in project_templates.items() if value},
    )

    for preset_name, preset_data in project.presets.items():
        preset_context = dict(combined_context)
        preset_environment = preset_data.get("environment", {}) if isinstance(preset_data, Mapping) else {}
        preset_definitions = preset_data.get("definitions", {}) if isinstance(preset_data, Mapping) else {}
        preset_context["preset"] = {
            "name": preset_name,
            "environment": dict(preset_environment) if isinstance(preset_environment, Mapping) else {},
            "definitions": dict(preset_definitions) if isinstance(preset_definitions, Mapping) else {},
        }

        values_to_check: dict[str, Any] = {}
        if isinstance(preset_environment, Mapping):
            _ensure_no_cycles(
                preset_environment,
                prefixes=("preset.environment.",),
                label=f"Preset '{preset_name}' environment",
            )
            values_to_check["environment"] = preset_environment
        if isinstance(preset_definitions, Mapping):
            _ensure_no_cycles(
                preset_definitions,
                prefixes=("preset.definitions.",),
                label=f"Preset '{preset_name}' definitions",
            )
            values_to_check["definitions"] = preset_definitions
        extra_config = preset_data.get("extra_config_args") if isinstance(preset_data, Mapping) else None
        extra_build = preset_data.get("extra_build_args") if isinstance(preset_data, Mapping) else None
        if isinstance(extra_config, Sequence) and not isinstance(extra_config, (str, bytes, bytearray)):
            values_to_check["extra_config_args"] = extra_config
        if isinstance(extra_build, Sequence) and not isinstance(extra_build, (str, bytes, bytearray)):
            values_to_check["extra_build_args"] = extra_build

        if values_to_check:
            validate_variables(context=preset_context, values=values_to_check)
def _validate_project_presets(project: ProjectDefinition, store: ConfigurationStore) -> list[str]:
    errors: list[str] = []
    available_presets = _collect_all_preset_names(store)
    if project.presets:
        _validate_preset_collection(
            project.presets,
            available_presets=available_presets,
            errors=errors,
            origin=f"project '{project.name}'",
        )
    return errors


def _collect_all_preset_names(store: ConfigurationStore) -> set[str]:
    names: set[str] = set()

    def _add_variants(base_name: str, *, org: str | None = None, project: str | None = None) -> None:
        names.add(base_name)
        if project:
            names.add(f"{project}/{base_name}")
        if org:
            names.add(f"{org}/{base_name}")
        if org and project:
            names.add(f"{org}/{project}/{base_name}")

    for project in store.projects.values():
        for raw_name, raw_definition in project.presets.items():
            base_name = str(raw_name)
            _add_variants(base_name, org=project.org, project=project.name)

            if not isinstance(raw_definition, Mapping):
                continue

            org_value = raw_definition.get("org")
            org_text = str(org_value).strip() if isinstance(org_value, str) else None
            org_text = org_text or None

            project_value = raw_definition.get("project")
            project_text = str(project_value).strip() if isinstance(project_value, str) else None
            project_text = project_text or None

            if org_text or project_text:
                _add_variants(base_name, org=org_text, project=project_text)

    for shared in store.shared_configs.values():
        presets_section = shared.get("presets")
        if not isinstance(presets_section, Mapping):
            continue
        for raw_name, raw_definition in presets_section.items():
            base_name = str(raw_name)
            names.add(base_name)

            if not isinstance(raw_definition, Mapping):
                continue

            org_value = raw_definition.get("org")
            org_text = str(org_value).strip() if isinstance(org_value, str) else None
            org_text = org_text or None

            project_value = raw_definition.get("project")
            project_text = str(project_value).strip() if isinstance(project_value, str) else None
            project_text = project_text or None

            if project_text:
                names.add(f"{project_text}/{base_name}")
            if org_text:
                names.add(f"{org_text}/{base_name}")
                if project_text:
                    names.add(f"{org_text}/{project_text}/{base_name}")

    return names


def _validate_preset_collection(
    presets: Mapping[str, Any],
    *,
    available_presets: set[str],
    errors: list[str],
    origin: str,
) -> None:
    for raw_name, raw_definition in presets.items():
        name = str(raw_name)
        label = f"Preset '{name}' ({origin})"
        if not isinstance(raw_definition, Mapping):
            errors.append(f"{label} must be a table/mapping")
            continue
        _validate_preset_definition(
            name,
            raw_definition,
            available_presets=available_presets,
            errors=errors,
            label=label,
        )


def _validate_preset_definition(
    name: str,
    data: Mapping[str, Any],
    *,
    available_presets: set[str],
    errors: list[str],
    label: str,
) -> None:
    extends_value = data.get("extends")
    extends = _parse_extends(extends_value, label=label, errors=errors)
    for target in extends:
        if _looks_like_template(target):
            continue
        if target not in available_presets and target != name:
            errors.append(f"{label} extends unknown preset '{target}'")

    condition_value = data.get("condition")
    if condition_value is not None:
        if isinstance(condition_value, str):
            _validate_expression(condition_value, source=f"{label} condition", errors=errors)
        else:
            errors.append(f"{label} condition must be a string expression")

    environment = data.get("environment")
    if isinstance(environment, Mapping):
        try:
            _ensure_no_cycles(
                environment,
                prefixes=("preset.environment.",),
                label=f"{label} environment",
            )
        except TemplateError as exc:
            errors.append(str(exc))
        _validate_embedded_expressions(environment, base_label=f"{label} environment", errors=errors)

    definitions = data.get("definitions")
    if isinstance(definitions, Mapping):
        try:
            _ensure_no_cycles(
                definitions,
                prefixes=("preset.definitions.",),
                label=f"{label} definitions",
            )
        except TemplateError as exc:
            errors.append(str(exc))
        _validate_embedded_expressions(definitions, base_label=f"{label} definitions", errors=errors)

    extra_config = data.get("extra_config_args")
    if isinstance(extra_config, Sequence) and not isinstance(extra_config, (str, bytes)):
        _validate_embedded_expressions(extra_config, base_label=f"{label} extra_config_args", errors=errors)

    extra_build = data.get("extra_build_args")
    if isinstance(extra_build, Sequence) and not isinstance(extra_build, (str, bytes)):
        _validate_embedded_expressions(extra_build, base_label=f"{label} extra_build_args", errors=errors)


def _parse_extends(value: Any, *, label: str, errors: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidate = value.strip()
        return (candidate,) if candidate else ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        result: list[str] = []
        for item in value:
            if not isinstance(item, str):
                errors.append(f"{label} extends entries must be strings")
                continue
            candidate = item.strip()
            if candidate:
                result.append(candidate)
        return tuple(result)
    errors.append(f"{label} extends must be a string or sequence of strings")
    return ()


def _looks_like_template(text: str) -> bool:
    return "{{" in text or "[[" in text


def _validate_expression(value: str, *, source: str, errors: list[str]) -> None:
    match = _EXPRESSION_PATTERN.match(value)
    if not match:
        errors.append(f"{source} must use the form [[ expression ]]")
        return

    expr = match.group("expr").strip()
    if not expr:
        errors.append(f"{source} must not be empty")
        return

    expr_for_parse = _PLACEHOLDER_PATTERN.sub("0", expr)
    try:
        validate_expression_syntax(expr_for_parse)
    except TemplateError as exc:
        errors.append(f"{source} expression error: {exc}")


def _validate_embedded_expressions(value: Any, *, base_label: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_embedded_expressions(item, base_label=f"{base_label}.{key}", errors=errors)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _validate_embedded_expressions(item, base_label=f"{base_label}[{index}]", errors=errors)
        return
    if isinstance(value, str) and _EXPRESSION_PATTERN.match(value):
        _validate_expression(value, source=base_label, errors=errors)


__all__ = ["validate_project", "validate_project_templates", "validate_store_structure"]
