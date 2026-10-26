"""Typed registries and the declared Bedrock particle component surface."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Generic, Iterable, Iterator, Mapping, Optional, Tuple, TypeVar


class DuplicateRegistration(ValueError):
    pass


T = TypeVar("T")


class Registry(Generic[T]):
    """Deterministic name registry with explicit duplicate handling."""

    def __init__(self, entries: Iterable[Tuple[str, T]] = ()) -> None:
        self._entries: Dict[str, T] = {}
        self._frozen = False
        for name, value in entries:
            self.register(name, value)

    def register(self, name: str, value: T, *, replace: bool = False) -> None:
        if self._frozen:
            raise RuntimeError("Registry is frozen")
        if not isinstance(name, str) or not name:
            raise ValueError("Registry name must be a non-empty string")
        if name in self._entries and not replace:
            raise DuplicateRegistration("Already registered: %s" % name)
        self._entries[name] = value

    def get(self, name: str, default: Optional[T] = None) -> Optional[T]:
        return self._entries.get(name, default)

    def require(self, name: str) -> T:
        try:
            return self._entries[name]
        except KeyError as exc:
            raise KeyError("Not registered: %s" % name) from exc

    def freeze(self) -> "Registry[T]":
        self._frozen = True
        return self

    @property
    def frozen(self) -> bool:
        return self._frozen

    def items(self) -> Tuple[Tuple[str, T], ...]:
        return tuple(self._entries.items())

    def names(self) -> Tuple[str, ...]:
        return tuple(self._entries)

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)


class ComponentDomain(str, Enum):
    EMITTER = "emitter"
    PARTICLE = "particle"
    RENDER = "render"


class EvaluationScope(str, Enum):
    EMITTER_CREATION = "emitter_creation"
    EMITTER_LOOP = "emitter_loop"
    EMITTER_UPDATE = "emitter_update"
    PARTICLE_CREATION = "particle_creation"
    PARTICLE_UPDATE = "particle_update"
    RENDER_FRAME = "render_frame"
    EVENT = "event"


class SupportLevel(str, Enum):
    """Honest implementation state for this new core.

    NORMALIZED means the component survives parsing in typed document IR, but no
    Bedrock simulation semantics are claimed yet.  INFRASTRUCTURE means the core
    implements a reusable primitive needed by the component.  IMPLEMENTED is
    reserved for behavior implemented and tested in this package.
    """

    NORMALIZED = "normalized"
    INFRASTRUCTURE = "infrastructure"
    IMPLEMENTED = "implemented"
    EXTENSION = "extension"


@dataclass(frozen=True)
class ComponentSpec:
    identifier: str
    domain: ComponentDomain
    scopes: Tuple[EvaluationScope, ...]
    support: SupportLevel = SupportLevel.NORMALIZED
    notes: str = ""


def _spec(
    identifier: str,
    domain: ComponentDomain,
    *scopes: EvaluationScope,
    support: SupportLevel = SupportLevel.NORMALIZED,
    notes: str = "",
) -> ComponentSpec:
    return ComponentSpec(identifier, domain, tuple(scopes), support, notes)


def create_default_component_registry() -> Registry[ComponentSpec]:
    """Return the current documented component vocabulary.

    Source: Mojang generated Particles documentation v1.21.90.3 and Microsoft
    Learn's particle component reference.  The registry describes coverage; it
    does not pretend normalized components already have simulation handlers.
    """

    emitter = ComponentDomain.EMITTER
    particle = ComponentDomain.PARTICLE
    render = ComponentDomain.RENDER
    ec = EvaluationScope.EMITTER_CREATION
    el = EvaluationScope.EMITTER_LOOP
    eu = EvaluationScope.EMITTER_UPDATE
    pc = EvaluationScope.PARTICLE_CREATION
    pu = EvaluationScope.PARTICLE_UPDATE
    rf = EvaluationScope.RENDER_FRAME
    ev = EvaluationScope.EVENT
    specs = [
        _spec("minecraft:emitter_initialization", emitter, ec, eu),
        _spec("minecraft:emitter_local_space", emitter, ec, pu),
        _spec("minecraft:emitter_rate_instant", emitter, el),
        _spec("minecraft:emitter_rate_manual", emitter, ev),
        _spec("minecraft:emitter_rate_steady", emitter, el, eu, pc),
        _spec("minecraft:emitter_lifetime_events", emitter, ec, el, eu, ev),
        _spec("minecraft:emitter_lifetime_expression", emitter, eu),
        _spec("minecraft:emitter_lifetime_looping", emitter, el, eu),
        _spec("minecraft:emitter_lifetime_once", emitter, ec, eu),
        _spec("minecraft:emitter_shape_disc", emitter, pc),
        _spec("minecraft:emitter_shape_box", emitter, pc),
        _spec("minecraft:emitter_shape_custom", emitter, pc),
        _spec("minecraft:emitter_shape_entity_aabb", emitter, pc),
        _spec("minecraft:emitter_shape_point", emitter, pc),
        _spec("minecraft:emitter_shape_sphere", emitter, pc),
        _spec("minecraft:particle_initial_speed", particle, pc),
        _spec("minecraft:particle_initial_spin", particle, pc),
        _spec("minecraft:particle_initialization", particle, pu, rf),
        _spec("minecraft:particle_motion_dynamic", particle, pu),
        _spec("minecraft:particle_motion_parametric", particle, pu),
        _spec("minecraft:particle_motion_collision", particle, pu, ev),
        _spec("minecraft:particle_lifetime_expression", particle, pc, pu),
        _spec("minecraft:particle_lifetime_events", particle, pc, pu, ev),
        _spec("minecraft:particle_expire_if_in_blocks", particle, pu),
        _spec("minecraft:particle_expire_if_not_in_blocks", particle, pu),
        _spec("minecraft:particle_kill_plane", particle, pu),
        _spec("minecraft:particle_appearance_billboard", render, rf),
        _spec("minecraft:particle_appearance_tinting", render, rf),
        _spec("minecraft:particle_appearance_lighting", render, rf),
        _spec(
            "minecraft:particle_appearance_stretched_billboard",
            render,
            rf,
            support=SupportLevel.EXTENSION,
            notes=(
                "Observed legacy extension; absent from the current generated "
                "component list. Preserve data, but do not claim Bedrock parity."
            ),
        ),
    ]
    registry: Registry[ComponentSpec] = Registry()
    for spec in specs:
        registry.register(spec.identifier, spec)
    return registry.freeze()


DEFAULT_COMPONENT_REGISTRY = create_default_component_registry()


@dataclass(frozen=True)
class CoverageRow:
    identifier: str
    support: SupportLevel
    scopes: Tuple[EvaluationScope, ...]
    notes: str = ""


def component_coverage(
    registry: Registry[ComponentSpec] = DEFAULT_COMPONENT_REGISTRY,
) -> Tuple[CoverageRow, ...]:
    return tuple(
        CoverageRow(spec.identifier, spec.support, spec.scopes, spec.notes)
        for _, spec in registry.items()
    )

