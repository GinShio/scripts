"""Toolchain configuration parsing and registry utilities."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, MutableMapping


def _to_str_dict(mapping: Mapping[str, Any]) -> Dict[str, str]:
    return {str(key): str(value) for key, value in mapping.items()}


@dataclass(slots=True)
class ToolchainBuildOverrides:
    environment: Dict[str, str] = field(default_factory=dict)
    definitions: Dict[str, Any] = field(default_factory=dict)
    launcher: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ToolchainBuildOverrides":
        allowed_keys = {"environment", "definitions", "launcher"}
        unknown = {str(key) for key in data.keys() if str(key) not in allowed_keys}
        if unknown:
            joined = ", ".join(sorted(unknown))
            raise ValueError(f"Toolchain override contains unknown keys: {joined}")
        environment: Dict[str, str] = {}
        definitions: Dict[str, Any] = {}
        launcher: str | None = None
        env_section = data.get("environment")
        if isinstance(env_section, Mapping):
            environment = _to_str_dict(env_section)
        defs_section = data.get("definitions")
        if isinstance(defs_section, Mapping):
            definitions = dict(defs_section)
        launcher_value = data.get("launcher")
        if isinstance(launcher_value, str) and launcher_value.strip():
            launcher = launcher_value.strip()
        return cls(environment=environment, definitions=definitions, launcher=launcher)

    def merge(self, other: "ToolchainBuildOverrides") -> "ToolchainBuildOverrides":
        environment = dict(self.environment)
        environment.update(other.environment)
        definitions = dict(self.definitions)
        definitions.update(other.definitions)
        launcher = other.launcher if other.launcher is not None else self.launcher
        return ToolchainBuildOverrides(environment=environment, definitions=definitions, launcher=launcher)

    def clone(self) -> "ToolchainBuildOverrides":
        return ToolchainBuildOverrides(
            environment=dict(self.environment),
            definitions=dict(self.definitions),
            launcher=self.launcher,
        )


@dataclass(slots=True)
class ToolchainDefinition:
    name: str
    description: str | None = None
    cc: str | None = None
    cxx: str | None = None
    rustc: str | None = None
    linker: str | None = None
    launcher: str | None = None
    environment: Dict[str, str] = field(default_factory=dict)
    definitions: Dict[str, Any] = field(default_factory=dict)
    build_overrides: Dict[str, ToolchainBuildOverrides] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    supported_build_systems: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(cls, name: str, data: Mapping[str, Any]) -> "ToolchainDefinition":
        if not isinstance(data, Mapping):
            raise TypeError(f"Toolchain '{name}' definition must be a mapping")

        allowed_keys = {
            "description",
            "cc",
            "cxx",
            "rustc",
            "linker",
            "launcher",
            "environment",
            "definitions",
            "build_systems",
            "metadata",
            "supports",
        }
        unknown = {str(key) for key in data.keys() if str(key) not in allowed_keys}
        if unknown:
            joined = ", ".join(sorted(unknown))
            raise ValueError(f"Toolchain '{name}' contains unknown keys: {joined}")

        description = data.get("description")
        cc = data.get("cc")
        cxx = data.get("cxx")
        rustc = data.get("rustc")
        linker = data.get("linker")
        launcher_value = data.get("launcher")
        launcher = None
        if isinstance(launcher_value, str) and launcher_value.strip():
            launcher = launcher_value.strip()

        environment: Dict[str, str] = {}
        env_section = data.get("environment")
        if isinstance(env_section, Mapping):
            environment = _to_str_dict(env_section)

        definitions: Dict[str, Any] = {}
        definitions_section = data.get("definitions")
        if isinstance(definitions_section, Mapping):
            definitions = dict(definitions_section)

        overrides: Dict[str, ToolchainBuildOverrides] = {}
        build_systems_section = data.get("build_systems")
        if isinstance(build_systems_section, Mapping):
            for raw_system, raw_data in build_systems_section.items():
                system_name = str(raw_system).strip().lower()
                if not system_name:
                    continue
                if isinstance(raw_data, Mapping):
                    overrides[system_name] = ToolchainBuildOverrides.from_mapping(raw_data)

        metadata_section = data.get("metadata")
        metadata = dict(metadata_section) if isinstance(metadata_section, Mapping) else {}

        supports_section = data.get("supports")
        supports: frozenset[str]
        if isinstance(supports_section, Iterable) and not isinstance(supports_section, (str, bytes)):
            supports = frozenset(str(item).strip().lower() for item in supports_section if str(item).strip())
        elif isinstance(supports_section, str):
            supports = frozenset({supports_section.strip().lower()}) if supports_section.strip() else frozenset()
        else:
            supports = frozenset()

        definition = cls(
            name=name,
            description=str(description) if description is not None else None,
            cc=str(cc) if cc is not None else None,
            cxx=str(cxx) if cxx is not None else None,
            rustc=str(rustc) if rustc is not None else None,
            linker=str(linker) if linker is not None else None,
            launcher=launcher,
            environment=environment,
            definitions=definitions,
            build_overrides=overrides,
            metadata=metadata,
            supported_build_systems=supports,
        )
        if definition.cc is None:
            definition.cc = definition.environment.get("CC")
        if definition.cxx is None:
            definition.cxx = definition.environment.get("CXX")
        if definition.rustc is None:
            definition.rustc = definition.environment.get("RUSTC")
        return definition

    def merge(self, other: "ToolchainDefinition") -> "ToolchainDefinition":
        environment = dict(self.environment)
        environment.update(other.environment)

        definitions = dict(self.definitions)
        definitions.update(other.definitions)

        overrides: Dict[str, ToolchainBuildOverrides] = {}
        for key, value in self.build_overrides.items():
            overrides[key] = value.clone()
        for key, value in other.build_overrides.items():
            if key in overrides:
                overrides[key] = overrides[key].merge(value)
            else:
                overrides[key] = value.clone()

        metadata = dict(self.metadata)
        metadata.update(other.metadata)

        supports = self.supported_build_systems
        if other.supported_build_systems:
            supports = other.supported_build_systems

        return ToolchainDefinition(
            name=self.name,
            description=other.description or self.description,
            cc=other.cc or self.cc,
            cxx=other.cxx or self.cxx,
            rustc=other.rustc or self.rustc,
            linker=other.linker or self.linker,
            launcher=other.launcher or self.launcher,
            environment=environment,
            definitions=definitions,
            build_overrides=overrides,
            metadata=metadata,
            supported_build_systems=supports,
        )

    def clone(self) -> "ToolchainDefinition":
        overrides = {name: override.clone() for name, override in self.build_overrides.items()}
        return ToolchainDefinition(
            name=self.name,
            description=self.description,
            cc=self.cc,
            cxx=self.cxx,
            rustc=self.rustc,
            linker=self.linker,
            launcher=self.launcher,
            environment=dict(self.environment),
            definitions=dict(self.definitions),
            build_overrides=overrides,
            metadata=dict(self.metadata),
            supported_build_systems=self.supported_build_systems,
        )

    @property
    def resolved_cc(self) -> str | None:
        return self.cc or self.environment.get("CC")

    @property
    def resolved_cxx(self) -> str | None:
        return self.cxx or self.environment.get("CXX")

    @property
    def resolved_linker(self) -> str | None:
        return self.linker or self.environment.get("LINKER")

    @property
    def resolved_launcher(self) -> str | None:
        return self.launcher

    def resolve_launcher(self, build_system: str | None) -> str | None:
        if build_system:
            overrides = self.build_overrides.get(build_system.lower())
            if overrides and overrides.launcher is not None:
                return overrides.launcher
        return self.launcher

    def apply(
        self,
        *,
        build_system: str | None,
        environment: MutableMapping[str, str],
        definitions: MutableMapping[str, Any],
        explicit: bool,
    ) -> None:
        self._apply_environment(environment, self.environment, explicit)
        self._apply_definitions(definitions, self.definitions, explicit)
        system_key = build_system.lower() if build_system else None
        if system_key:
            overrides = self.build_overrides.get(system_key)
            if overrides:
                self._apply_environment(environment, overrides.environment, explicit)
                self._apply_definitions(definitions, overrides.definitions, explicit)

    @staticmethod
    def _apply_environment(
        target: MutableMapping[str, str],
        source: Mapping[str, str],
        explicit: bool,
    ) -> None:
        for key, value in source.items():
            if explicit or key not in target or target.get(key) != value:
                target[key] = value

    @staticmethod
    def _apply_definitions(
        target: MutableMapping[str, Any],
        source: Mapping[str, Any],
        explicit: bool,
    ) -> None:
        for key, value in source.items():
            if explicit or key not in target or target.get(key) != value:
                target[key] = value


def _build_builtin_definitions() -> Dict[str, ToolchainDefinition]:
    raw: Dict[str, Mapping[str, Any]] = {
        "clang": {
            "description": "LLVM Clang toolchain",
            "supports": ["cmake", "meson", "bazel", "make"],
            "environment": {
                "CC": "clang",
                "CXX": "clang++",
                "CPP": "clang -E",
                "AR": "llvm-ar",
                "NM": "llvm-nm",
                "RANLIB": "llvm-ranlib",
                "STRIP": "llvm-strip",
            },
            "build_systems": {
                "cmake": {
                    "definitions": {
                        "CMAKE_C_COMPILER": "clang",
                        "CMAKE_CXX_COMPILER": "clang++",
                        "CMAKE_AR": "llvm-ar",
                        "CMAKE_RANLIB": "llvm-ranlib",
                    }
                }
            },
        },
        "gcc": {
            "description": "GNU Compiler Collection",
            "supports": ["cmake", "meson", "bazel", "make"],
            "environment": {
                "CC": "gcc",
                "CXX": "g++",
                "CPP": "gcc -E",
                "AR": "gcc-ar",
                "NM": "gcc-nm",
                "RANLIB": "gcc-ranlib",
                "STRIP": "strip",
            },
            "build_systems": {
                "cmake": {
                    "definitions": {
                        "CMAKE_C_COMPILER": "gcc",
                        "CMAKE_CXX_COMPILER": "g++",
                        "CMAKE_AR": "gcc-ar",
                        "CMAKE_RANLIB": "gcc-ranlib",
                    }
                }
            },
        },
        "msvc": {
            "description": "Microsoft Visual C++",
            "supports": ["cmake", "meson"],
            "environment": {
                "CC": "cl",
                "CXX": "cl",
                "AR": "lib",
                "RC": "rc",
            },
            "build_systems": {
                "cmake": {
                    "definitions": {
                        "CMAKE_C_COMPILER": "cl",
                        "CMAKE_CXX_COMPILER": "cl",
                        "CMAKE_RC_COMPILER": "rc",
                    }
                }
            },
        },
        "rustc": {
            "description": "Rust toolchain",
            "supports": ["cargo"],
            "environment": {
                "RUSTC": "rustc",
                "CARGO": "cargo",
            },
            "build_systems": {
                "cargo": {
                    "environment": {
                        "RUSTC": "rustc",
                    }
                }
            },
        },
    }

    definitions: Dict[str, ToolchainDefinition] = {}
    for name, data in raw.items():
        definitions[name] = ToolchainDefinition.from_mapping(name, data)
    return definitions


class ToolchainRegistry:
    def __init__(self, definitions: Mapping[str, ToolchainDefinition] | None = None) -> None:
        self._definitions: Dict[str, ToolchainDefinition] = {}
        if definitions:
            for name, definition in definitions.items():
                self._definitions[name] = definition.clone()

    @classmethod
    def with_builtins(cls) -> "ToolchainRegistry":
        return cls(_build_builtin_definitions())

    def merge(self, definitions: Mapping[str, ToolchainDefinition]) -> None:
        for name, definition in definitions.items():
            existing = self._definitions.get(name)
            if existing:
                self._definitions[name] = existing.merge(definition)
            else:
                self._definitions[name] = definition.clone()

    def merge_from_mapping(self, mapping: Mapping[str, Any]) -> None:
        if not mapping:
            return
        toolchains_section = mapping.get("toolchains")
        if isinstance(toolchains_section, Mapping):
            candidates = toolchains_section
        else:
            candidates = mapping
        parsed: Dict[str, ToolchainDefinition] = {}
        for raw_name, raw_value in candidates.items():
            name = str(raw_name).strip()
            if not name:
                continue
            normalized = name.lower()
            if isinstance(raw_value, Mapping):
                parsed[normalized] = ToolchainDefinition.from_mapping(name, raw_value)
        if parsed:
            self.merge(parsed)

    def get(self, name: str) -> ToolchainDefinition | None:
        definition = self._definitions.get(name.lower())
        return definition.clone() if definition else None

    def available(self) -> Iterable[str]:
        return self._definitions.keys()

    def validate(self) -> list[str]:
        errors: list[str] = []
        allowed_systems = {"cmake", "meson", "bazel", "cargo", "make"}
        for name, definition in self._definitions.items():
            if not (definition.resolved_cc or definition.resolved_cxx or definition.rustc):
                errors.append(f"Toolchain '{name}' must specify at least one compiler (cc/cxx/rustc)")
            for system_name in definition.build_overrides.keys():
                if system_name not in allowed_systems:
                    errors.append(
                        f"Toolchain '{name}' override references unsupported build system '{system_name}'"
                    )
        return errors


BUILTIN_TOOLCHAINS = _build_builtin_definitions()

__all__ = ["ToolchainDefinition", "ToolchainBuildOverrides", "ToolchainRegistry", "BUILTIN_TOOLCHAINS"]
