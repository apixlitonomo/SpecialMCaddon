"""Deterministic simulation scaffolding and host-service provider contracts.

This module intentionally does not duplicate the legacy simulator's guessed
component behavior.  It supplies stable IDs/randomness, fixed-step scheduling,
state snapshots, component cadence hooks, and adapters for Molang/world data.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Protocol, Tuple

from .diagnostics import Diagnostic, DiagnosticBag, Severity, SourceRef
from .ir import ComponentIR, ParticleDocumentIR, thaw_json
from .registry import DEFAULT_COMPONENT_REGISTRY


Vec3 = Tuple[float, float, float]
Quaternion = Tuple[float, float, float, float]


def _vec3(value: Iterable[float]) -> Vec3:
    result = tuple(float(item) for item in value)
    if len(result) != 3 or not all(math.isfinite(item) for item in result):
        raise ValueError("Expected a finite 3D vector")
    return result  # type: ignore[return-value]


def _stable_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Random tokens must be finite")
        return {"float": value.hex()}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return {"type": type(value).__qualname__, "value": str(value)}


class StableRandom:
    """Order-independent keyed random source.

    Results are a pure function of seed + domain tokens, so adding a new random
    query in one component cannot perturb unrelated particles.
    """

    def __init__(self, seed: int | str | bytes = 0) -> None:
        payload = json.dumps(
            _stable_value(seed), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        self._key = hashlib.sha256(payload).digest()

    def sample(self, *tokens: object) -> float:
        payload = json.dumps(
            _stable_value(tokens), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        digest = hashlib.blake2b(payload, key=self._key, digest_size=8).digest()
        integer = int.from_bytes(digest, "big")
        return integer / float(1 << 64)

    def uniform(self, minimum: float, maximum: float, *tokens: object) -> float:
        return float(minimum) + (
            float(maximum) - float(minimum)
        ) * self.sample(*tokens)

    def integer(self, minimum: int, maximum: int, *tokens: object) -> int:
        if maximum < minimum:
            raise ValueError("maximum must be >= minimum")
        width = maximum - minimum + 1
        return minimum + min(int(self.sample(*tokens) * width), width - 1)

    def signed(self, *tokens: object) -> float:
        return self.sample(*tokens) * 2.0 - 1.0

    def particle_randoms(
        self, effect_identifier: str, emitter_id: int, particle_id: int
    ) -> Tuple[float, float, float, float]:
        return tuple(
            self.sample(
                "particle-random",
                effect_identifier,
                emitter_id,
                particle_id,
                channel,
            )
            for channel in range(1, 5)
        )  # type: ignore[return-value]


class EvaluationScope(str, Enum):
    EMITTER_CREATION = "emitter_creation"
    EMITTER_LOOP = "emitter_loop"
    EMITTER_UPDATE = "emitter_update"
    PARTICLE_CREATION = "particle_creation"
    PARTICLE_UPDATE = "particle_update"
    RENDER_FRAME = "render_frame"
    EVENT = "event"


@dataclass
class EvaluationContext:
    scope: EvaluationScope
    emitter_id: int
    particle_id: Optional[int]
    time: float
    delta_time: float
    variables: Dict[str, Any]
    source: SourceRef = SourceRef()


class ExpressionUnavailable(ValueError):
    pass


class ExpressionProvider(Protocol):
    def evaluate(self, expression: Any, context: EvaluationContext) -> float:
        ...


class LiteralExpressionProvider:
    """Strict fallback; a full Molang implementation is injected by the host."""

    def evaluate(self, expression: Any, context: EvaluationContext) -> float:
        del context
        if isinstance(expression, bool):
            return 1.0 if expression else 0.0
        if isinstance(expression, (int, float)):
            result = float(expression)
            if math.isfinite(result):
                return result
        if isinstance(expression, str):
            text = expression.strip().lower()
            if text == "true":
                return 1.0
            if text == "false":
                return 0.0
            try:
                result = float(text)
            except ValueError as exc:
                raise ExpressionUnavailable(
                    "A Molang provider is required for %r" % expression
                ) from exc
            if math.isfinite(result):
                return result
        raise ExpressionUnavailable(
            "Expression is not a finite literal: %r" % (expression,)
        )


@dataclass(frozen=True)
class Transform:
    position: Vec3 = (0.0, 0.0, 0.0)
    rotation: Quaternion = (1.0, 0.0, 0.0, 0.0)
    scale: Vec3 = (1.0, 1.0, 1.0)
    velocity: Vec3 = (0.0, 0.0, 0.0)


class TransformProvider(Protocol):
    def sample(self, emitter_id: int, time: float) -> Transform:
        ...


class StaticTransformProvider:
    def __init__(self, transforms: Optional[Mapping[int, Transform]] = None) -> None:
        self.transforms = dict(transforms or {})

    def sample(self, emitter_id: int, time: float) -> Transform:
        del time
        return self.transforms.get(emitter_id, Transform())


@dataclass(frozen=True)
class CollisionHit:
    fraction: float
    position: Vec3
    normal: Vec3
    block_identifier: str = ""


class CollisionProvider(Protocol):
    def sweep(
        self, start: Vec3, end: Vec3, radius: float, context: EvaluationContext
    ) -> Optional[CollisionHit]:
        ...


class NullCollisionProvider:
    def sweep(
        self, start: Vec3, end: Vec3, radius: float, context: EvaluationContext
    ) -> Optional[CollisionHit]:
        del start, end, radius, context
        return None


class EnvironmentProvider(Protocol):
    def block_at(self, position: Vec3, context: EvaluationContext) -> str:
        ...

    def light_at(self, position: Vec3, context: EvaluationContext) -> float:
        ...


class NullEnvironmentProvider:
    def block_at(self, position: Vec3, context: EvaluationContext) -> str:
        del position, context
        return "minecraft:air"

    def light_at(self, position: Vec3, context: EvaluationContext) -> float:
        del position, context
        return 1.0


@dataclass
class RuntimeProviders:
    expressions: ExpressionProvider = field(
        default_factory=LiteralExpressionProvider
    )
    transforms: TransformProvider = field(default_factory=StaticTransformProvider)
    collisions: CollisionProvider = field(default_factory=NullCollisionProvider)
    environment: EnvironmentProvider = field(
        default_factory=NullEnvironmentProvider
    )


class SimulationPhase(str, Enum):
    EMITTER_CREATION = "emitter_creation"
    EMITTER_LOOP = "emitter_loop"
    EMITTER_UPDATE = "emitter_update"
    PARTICLE_CREATION = "particle_creation"
    PARTICLE_UPDATE = "particle_update"
    RENDER_SAMPLE = "render_sample"


@dataclass(frozen=True)
class ComponentHandlerSpec:
    component_identifier: str
    phase: SimulationPhase
    handler: Callable[["ComponentContext", Any], None]
    priority: int = 0


class ComponentRuntimeRegistry:
    """Cadence-aware component handler registry for builder integration."""

    def __init__(self) -> None:
        self._handlers: Dict[
            Tuple[str, SimulationPhase], ComponentHandlerSpec
        ] = {}

    def register(
        self,
        component_identifier: str,
        phase: SimulationPhase,
        handler: Callable[["ComponentContext", Any], None],
        *,
        priority: int = 0,
        replace: bool = False,
    ) -> None:
        key = (component_identifier, phase)
        if key in self._handlers and not replace:
            raise ValueError(
                "Handler already registered for %s at %s"
                % (component_identifier, phase.value)
            )
        self._handlers[key] = ComponentHandlerSpec(
            component_identifier, phase, handler, priority
        )

    def get(
        self, component_identifier: str, phase: SimulationPhase
    ) -> Optional[ComponentHandlerSpec]:
        return self._handlers.get((component_identifier, phase))

    def phases_for(self, component_identifier: str) -> Tuple[SimulationPhase, ...]:
        return tuple(
            spec.phase
            for spec in sorted(
                (
                    value
                    for (identifier, _), value in self._handlers.items()
                    if identifier == component_identifier
                ),
                key=lambda item: (item.phase.value, item.priority),
            )
        )


@dataclass
class ParticleState:
    particle_id: int
    age: float = 0.0
    lifetime: float = 1.0
    position: Vec3 = (0.0, 0.0, 0.0)
    velocity: Vec3 = (0.0, 0.0, 0.0)
    rotation: float = 0.0
    rotation_rate: float = 0.0
    alive: bool = True
    variables: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EmitterState:
    emitter_id: int
    effect: ParticleDocumentIR
    age: float = 0.0
    loop_age: float = 0.0
    loop_index: int = 0
    alive: bool = True
    emitting: bool = True
    variables: Dict[str, Any] = field(default_factory=dict)
    particles: Dict[int, ParticleState] = field(default_factory=dict)


@dataclass
class ComponentContext:
    simulation: "DeterministicSimulation"
    emitter: EmitterState
    particle: Optional[ParticleState]
    phase: SimulationPhase
    delta_time: float

    def evaluation_context(self, source: SourceRef = SourceRef()) -> EvaluationContext:
        variables = dict(self.emitter.variables)
        variables.setdefault("variable.emitter_age", self.emitter.age)
        if self.particle is not None:
            variables.update(self.particle.variables)
            variables.setdefault("variable.particle_age", self.particle.age)
            variables.setdefault("variable.particle_lifetime", self.particle.lifetime)
        runtime_scope = (
            EvaluationScope.RENDER_FRAME
            if self.phase == SimulationPhase.RENDER_SAMPLE
            else EvaluationScope(self.phase.value)
        )
        return EvaluationContext(
            runtime_scope,
            self.emitter.emitter_id,
            self.particle.particle_id if self.particle else None,
            self.simulation.time,
            self.delta_time,
            variables,
            source,
        )


@dataclass(frozen=True)
class SimulationConfig:
    fixed_step: float = 1.0 / 20.0
    max_particles_per_emitter: int = 16384
    max_emitters: int = 1024
    max_steps_per_advance: int = 10000

    def __post_init__(self) -> None:
        if not math.isfinite(self.fixed_step) or self.fixed_step <= 0:
            raise ValueError("fixed_step must be positive and finite")
        for name in (
            "max_particles_per_emitter",
            "max_emitters",
            "max_steps_per_advance",
        ):
            if getattr(self, name) < 1:
                raise ValueError("%s must be positive" % name)


@dataclass(frozen=True)
class ParticleSnapshot:
    particle_id: int
    age: float
    lifetime: float
    position: Vec3
    velocity: Vec3
    rotation: float
    rotation_rate: float
    alive: bool
    variables: Mapping[str, Any]


@dataclass(frozen=True)
class EmitterSnapshot:
    emitter_id: int
    effect_identifier: str
    age: float
    loop_age: float
    loop_index: int
    emitting: bool
    variables: Mapping[str, Any]
    particles: Tuple[ParticleSnapshot, ...]


@dataclass(frozen=True)
class SimulationSnapshot:
    tick: int
    time: float
    emitters: Tuple[EmitterSnapshot, ...]


@dataclass(frozen=True)
class RuntimeCoverageRow:
    component_identifier: str
    phases: Tuple[SimulationPhase, ...]
    simulated: bool


class DeterministicSimulation:
    def __init__(
        self,
        *,
        seed: int | str | bytes = 0,
        config: SimulationConfig = SimulationConfig(),
        providers: Optional[RuntimeProviders] = None,
        handlers: Optional[ComponentRuntimeRegistry] = None,
    ) -> None:
        self.config = config
        self.providers = providers or RuntimeProviders()
        self.handlers = handlers or ComponentRuntimeRegistry()
        self.random = StableRandom(seed)
        self.tick = 0
        self.time = 0.0
        self._remainder = 0.0
        self._next_emitter_id = 1
        self._next_particle_id = 1
        self.emitters: Dict[int, EmitterState] = {}
        self.diagnostics = DiagnosticBag()

    def add_emitter(
        self,
        effect: ParticleDocumentIR,
        *,
        variables: Optional[Mapping[str, Any]] = None,
    ) -> EmitterState:
        if len(self.emitters) >= self.config.max_emitters:
            raise RuntimeError("Emitter budget exceeded")
        emitter = EmitterState(
            self._next_emitter_id,
            effect,
            variables=dict(variables or {}),
        )
        self._next_emitter_id += 1
        emitter.variables.update(
            {
                "variable.emitter_random_1": self.random.sample(
                    "emitter-random", effect.description.identifier, emitter.emitter_id, 1
                ),
                "variable.emitter_random_2": self.random.sample(
                    "emitter-random", effect.description.identifier, emitter.emitter_id, 2
                ),
                "variable.emitter_random_3": self.random.sample(
                    "emitter-random", effect.description.identifier, emitter.emitter_id, 3
                ),
                "variable.emitter_random_4": self.random.sample(
                    "emitter-random", effect.description.identifier, emitter.emitter_id, 4
                ),
            }
        )
        self.emitters[emitter.emitter_id] = emitter
        self._run_phase(emitter, None, SimulationPhase.EMITTER_CREATION, 0.0)
        return emitter

    def spawn_particle(
        self,
        emitter_id: int,
        *,
        lifetime: float = 1.0,
        position: Iterable[float] = (0.0, 0.0, 0.0),
        velocity: Iterable[float] = (0.0, 0.0, 0.0),
        variables: Optional[Mapping[str, Any]] = None,
    ) -> Optional[ParticleState]:
        emitter = self.emitters[emitter_id]
        active_count = sum(1 for particle in emitter.particles.values() if particle.alive)
        if active_count >= self.config.max_particles_per_emitter:
            self.diagnostics.add(
                "particle.runtime.particle_budget",
                "Particle budget exceeded for emitter %d" % emitter_id,
                Severity.WARNING,
                emitter.effect.source,
            )
            return None
        lifetime_value = float(lifetime)
        if not math.isfinite(lifetime_value) or lifetime_value < 0:
            raise ValueError("Particle lifetime must be finite and non-negative")
        particle = ParticleState(
            particle_id=self._next_particle_id,
            lifetime=lifetime_value,
            position=_vec3(position),
            velocity=_vec3(velocity),
            variables=dict(variables or {}),
        )
        self._next_particle_id += 1
        random_values = self.random.particle_randoms(
            emitter.effect.description.identifier,
            emitter_id,
            particle.particle_id,
        )
        for channel, value in enumerate(random_values, 1):
            particle.variables.setdefault(
                "variable.particle_random_%d" % channel, value
            )
        emitter.particles[particle.particle_id] = particle
        self._run_phase(
            emitter, particle, SimulationPhase.PARTICLE_CREATION, 0.0
        )
        return particle

    def begin_emitter_loop(self, emitter_id: int) -> None:
        """Begin a loop and run component handlers at loop cadence."""

        emitter = self.emitters[emitter_id]
        emitter.loop_index += 1
        emitter.loop_age = 0.0
        self._run_phase(emitter, None, SimulationPhase.EMITTER_LOOP, 0.0)

    def advance(self, seconds: float) -> int:
        seconds_value = float(seconds)
        if not math.isfinite(seconds_value) or seconds_value < 0:
            raise ValueError("advance seconds must be finite and non-negative")
        self._remainder += seconds_value
        steps = int(
            math.floor((self._remainder + self.config.fixed_step * 1e-12)
                       / self.config.fixed_step)
        )
        if steps > self.config.max_steps_per_advance:
            raise RuntimeError("Simulation step budget exceeded")
        for _ in range(steps):
            self.step()
        self._remainder -= steps * self.config.fixed_step
        if abs(self._remainder) < self.config.fixed_step * 1e-12:
            self._remainder = 0.0
        return steps

    def step(self) -> None:
        dt = self.config.fixed_step
        for emitter_id in sorted(tuple(self.emitters)):
            emitter = self.emitters[emitter_id]
            if not emitter.alive:
                continue
            self._run_phase(
                emitter, None, SimulationPhase.EMITTER_UPDATE, dt
            )
            for particle_id in sorted(tuple(emitter.particles)):
                particle = emitter.particles[particle_id]
                if not particle.alive:
                    continue
                self._run_phase(
                    emitter, particle, SimulationPhase.PARTICLE_UPDATE, dt
                )
                particle.age += dt
                if particle.age >= particle.lifetime:
                    particle.alive = False
            emitter.age += dt
            emitter.loop_age += dt
        self.tick += 1
        self.time = self.tick * dt

    def sample_render(self) -> SimulationSnapshot:
        """Evaluate render-sample handlers before returning an alive snapshot."""

        for emitter_id in sorted(tuple(self.emitters)):
            emitter = self.emitters[emitter_id]
            if not emitter.alive:
                continue
            for particle_id in sorted(tuple(emitter.particles)):
                particle = emitter.particles[particle_id]
                if particle.alive:
                    self._run_phase(
                        emitter,
                        particle,
                        SimulationPhase.RENDER_SAMPLE,
                        0.0,
                    )
        return self.snapshot()

    def _run_phase(
        self,
        emitter: EmitterState,
        particle: Optional[ParticleState],
        phase: SimulationPhase,
        delta_time: float,
    ) -> None:
        context = ComponentContext(self, emitter, particle, phase, delta_time)
        for component in emitter.effect.components:
            spec = self.handlers.get(component.identifier, phase)
            if spec is None:
                continue
            try:
                spec.handler(context, thaw_json(component.payload))
            except Exception as exc:
                self.diagnostics.add(
                    "particle.runtime.component_handler",
                    "%s handler failed at %s: %s"
                    % (component.identifier, phase.value, exc),
                    Severity.ERROR,
                    component.source,
                )

    def snapshot(self, *, include_dead: bool = False) -> SimulationSnapshot:
        emitters = []
        for emitter_id in sorted(self.emitters):
            emitter = self.emitters[emitter_id]
            if not include_dead and not emitter.alive:
                continue
            particles = tuple(
                ParticleSnapshot(
                    particle.particle_id,
                    particle.age,
                    particle.lifetime,
                    particle.position,
                    particle.velocity,
                    particle.rotation,
                    particle.rotation_rate,
                    particle.alive,
                    dict(particle.variables),
                )
                for particle in (
                    emitter.particles[particle_id]
                    for particle_id in sorted(emitter.particles)
                )
                if include_dead or particle.alive
            )
            emitters.append(
                EmitterSnapshot(
                    emitter.emitter_id,
                    emitter.effect.description.identifier,
                    emitter.age,
                    emitter.loop_age,
                    emitter.loop_index,
                    emitter.emitting,
                    dict(emitter.variables),
                    particles,
                )
            )
        return SimulationSnapshot(self.tick, self.time, tuple(emitters))

    def coverage(self) -> Tuple[RuntimeCoverageRow, ...]:
        rows = []
        for identifier in DEFAULT_COMPONENT_REGISTRY:
            phases = self.handlers.phases_for(identifier)
            rows.append(RuntimeCoverageRow(identifier, phases, bool(phases)))
        return tuple(rows)

    @property
    def diagnostics_snapshot(self) -> Tuple[Diagnostic, ...]:
        return self.diagnostics.snapshot()

