"""Context builders for template and expression evaluation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping
import os
import platform


@dataclass(slots=True)
class UserContext:
    branch: str
    branch_raw: str
    branch_slug: str
    build_type: str
    generator: str | None = None
    operation: str | None = None
    toolchain: str | None = None
    linker: str | None = None
    cc: str | None = None
    cxx: str | None = None
    launcher: str | None = None

    def to_mapping(self) -> Dict[str, Any]:
        data = {
            "branch": self.branch,
            "branch_raw": self.branch_raw,
            "branch_slug": self.branch_slug,
            "build_type": self.build_type,
        }
        optional = {
            "generator": self.generator,
            "operation": self.operation,
            "toolchain": self.toolchain,
            "linker": self.linker,
            "cc": self.cc,
            "cxx": self.cxx,
            "launcher": self.launcher,
        }
        for key, value in optional.items():
            if value is not None:
                data[key] = value
        return data


@dataclass(slots=True)
class ProjectContext:
    name: str
    source_dir: Path
    build_dir: Path | None
    install_dir: Path | None = None
    component_dir: Path | None = None
    environment: Mapping[str, str] | None = None

    def to_mapping(self) -> Dict[str, Any]:
        data = {
            "name": self.name,
            "source_dir": str(self.source_dir),
        }
        optional = {
            "build_dir": self.build_dir,
            "install_dir": self.install_dir,
            "component_dir": self.component_dir,
        }
        for key, value in optional.items():
            if value is not None:
                data[key] = str(value)
        if self.environment:
            data["environment"] = dict(self.environment)
        return data


@dataclass(slots=True)
class SystemContext:
    os_name: str
    architecture: str
    memory_total_gb: int | None

    def to_mapping(self) -> Dict[str, Any]:
        data = {
            "os": self.os_name,
            "architecture": self.architecture,
        }
        if self.memory_total_gb is not None:
            data["memory"] = {"total_gb": self.memory_total_gb}
        return data


class ContextBuilder:
    """Builds the variable context for templating and expressions."""

    def __init__(self, builder_path: Path, env: Mapping[str, str] | None = None) -> None:
        self._builder_path = builder_path
        self._env = dict(env) if env is not None else dict(os.environ)

    def user(
        self,
        branch: str,
        build_type: str,
        generator: str | None,
        operation: str | None,
        *,
        toolchain: str | None = None,
        linker: str | None = None,
        cc: str | None = None,
        cxx: str | None = None,
        launcher: str | None = None,
    ) -> UserContext:
        sanitized_branch = branch.replace("/", "_")
        return UserContext(
            branch=sanitized_branch,
            branch_raw=branch,
            branch_slug=sanitized_branch,
            build_type=build_type,
            generator=generator,
            operation=operation,
            toolchain=toolchain,
            linker=linker,
            cc=cc,
            cxx=cxx,
            launcher=launcher,
        )

    def system(self) -> SystemContext:
        os_name = platform.system().lower()
        architecture = platform.machine()
        memory_total_gb: int | None = None
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names and "SC_PHYS_PAGES" in os.sysconf_names:  # type: ignore[attr-defined]
            try:
                page_size = os.sysconf("SC_PAGE_SIZE")  # type: ignore[arg-type]
                pages = os.sysconf("SC_PHYS_PAGES")  # type: ignore[arg-type]
                memory_bytes = page_size * pages
                memory_total_gb = max(1, int(memory_bytes / (1024 ** 3)))
            except (ValueError, OSError):
                memory_total_gb = None
        return SystemContext(os_name=os_name, architecture=architecture, memory_total_gb=memory_total_gb)

    def project(
        self,
        *,
        name: str,
        source_dir: Path,
        build_dir: Path | None,
        install_dir: Path | None,
        component_dir: Path | None,
        environment: Mapping[str, str] | None = None,
    ) -> ProjectContext:
        return ProjectContext(
            name=name,
            source_dir=source_dir,
            build_dir=build_dir,
            install_dir=install_dir,
            component_dir=component_dir,
            environment=environment,
        )

    def environment(self) -> Mapping[str, str]:
        return self._env

    def combined_context(
        self,
        *,
        user: UserContext,
        project: ProjectContext,
        system: SystemContext,
    ) -> Dict[str, Any]:
        return {
            "user": user.to_mapping(),
            "project": project.to_mapping(),
            "system": system.to_mapping(),
            "env": dict(self._env),
            "builder": {"path": str(self._builder_path)},
        }
