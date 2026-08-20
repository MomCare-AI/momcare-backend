"""First-class program registry.

A *program* (a feature module — the future maternal-health monitoring module,
eventually billing/portal/EHR) is a group of feature apps that can be
activated for a hospital. Programs register themselves with this registry
from their own ``AppConfig.ready()`` — so ``core`` never imports a module,
the dependency direction is always ``modules -> core`` (blueprint §5/§6).

Deliberately minimal: unlike Neuro_RPM's version of this file, there is no
clinical ``ProgramCode`` enum here — that's medical-domain content that
belongs in the first feature module's own design, not the foundation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProgramSpec:
    key: str  # registry key, must match ModuleRegistry.module_key
    display_name: str
    django_app_labels: list[str]  # the feature apps this program owns
    router_factory: Callable[[], object]  # returns a DRF router with this program's routes
    requires: tuple[str, ...] = field(default_factory=tuple)  # other program keys this depends on


_REGISTRY: dict[str, ProgramSpec] = {}


def register_program(spec: ProgramSpec) -> None:
    if spec.key in _REGISTRY:
        msg = f"Program '{spec.key}' is already registered"
        raise ValueError(msg)
    _REGISTRY[spec.key] = spec


def iter_programs() -> list[ProgramSpec]:
    return list(_REGISTRY.values())


def get_program(key: str) -> ProgramSpec | None:
    return _REGISTRY.get(key)
