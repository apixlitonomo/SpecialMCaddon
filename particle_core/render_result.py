"""Canonical renderer contract for particle-core consumers.

The builder should consume this IR rather than inspect Bedrock components.  All
positions/directions remain in Bedrock's right-handed Y-up space; a renderer
performs one coordinate conversion at its boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple

from .diagnostics import Diagnostic
from .ir import FrozenDict, freeze_json
from .runtime import EmitterSnapshot, ParticleSnapshot, SimulationSnapshot


Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]
Vec4 = Tuple[float, float, float, float]


def _finite_tuple(value: Tuple[float, ...], length: int, label: str) -> None:
    if len(value) != length or not all(math.isfinite(float(item)) for item in value):
        raise ValueError("%s must contain %d finite numbers" % (label, length))


@dataclass(frozen=True)
class EffectRenderMetadata:
    """Effect-wide data needed to construct a material and billboard basis."""

    material: str
    texture: str
    face_camera_mode: str = "rotate_xyz"
    direction_mode: str = "derive_from_velocity"
    min_speed_threshold: float = 0.01
    custom_direction: Optional[Vec3] = None
    texture_dimensions: Tuple[int, int] = (1, 1)
    lighting: bool = False
    color_space: str = "bedrock_rgba"
    extra: FrozenDict = FrozenDict()

    def __post_init__(self) -> None:
        if not self.material:
            raise ValueError("material must not be empty")
        if not self.texture:
            raise ValueError("texture must not be empty")
        if (
            len(self.texture_dimensions) != 2
            or self.texture_dimensions[0] <= 0
            or self.texture_dimensions[1] <= 0
        ):
            raise ValueError("texture_dimensions must be positive")
        if (
            not math.isfinite(self.min_speed_threshold)
            or self.min_speed_threshold < 0
        ):
            raise ValueError("min_speed_threshold must be finite and non-negative")
        if self.custom_direction is not None:
            _finite_tuple(self.custom_direction, 3, "custom_direction")
        if not isinstance(self.extra, FrozenDict):
            object.__setattr__(self, "extra", freeze_json(self.extra))


@dataclass(frozen=True)
class ParticleAppearance:
    """Renderer attributes evaluated for one particle at one sample time."""

    size_half_extents: Vec2 = (0.5, 0.5)
    rgba: Vec4 = (1.0, 1.0, 1.0, 1.0)
    uv_rect: Vec4 = (0.0, 0.0, 1.0, 1.0)
    rotation_degrees: float = 0.0
    direction: Vec3 = (0.0, 0.0, 0.0)
    alive: bool = True

    def __post_init__(self) -> None:
        _finite_tuple(self.size_half_extents, 2, "size_half_extents")
        _finite_tuple(self.rgba, 4, "rgba")
        _finite_tuple(self.uv_rect, 4, "uv_rect")
        _finite_tuple(self.direction, 3, "direction")
        if not math.isfinite(self.rotation_degrees):
            raise ValueError("rotation_degrees must be finite")


@dataclass(frozen=True)
class CanonicalParticleSample:
    particle_id: int
    frame: int
    time: float
    bedrock_position: Vec3
    size_half_extents: Vec2
    rgba: Vec4
    uv_rect: Vec4
    rotation_degrees: float
    direction: Vec3
    alive: bool

    def __post_init__(self) -> None:
        if self.particle_id < 1:
            raise ValueError("particle_id must be positive")
        if self.frame < 0:
            raise ValueError("frame must be non-negative")
        if not math.isfinite(self.time) or self.time < 0:
            raise ValueError("time must be finite and non-negative")
        _finite_tuple(self.bedrock_position, 3, "bedrock_position")
        _finite_tuple(self.size_half_extents, 2, "size_half_extents")
        _finite_tuple(self.rgba, 4, "rgba")
        _finite_tuple(self.uv_rect, 4, "uv_rect")
        _finite_tuple(self.direction, 3, "direction")
        if not math.isfinite(self.rotation_degrees):
            raise ValueError("rotation_degrees must be finite")


@dataclass(frozen=True)
class ParticleTrack:
    particle_id: int
    samples: Tuple[CanonicalParticleSample, ...]

    def __post_init__(self) -> None:
        previous = None
        for sample in self.samples:
            if sample.particle_id != self.particle_id:
                raise ValueError("Track contains a different particle ID")
            key = (sample.frame, sample.time)
            if previous is not None and key <= previous:
                raise ValueError("Track samples must be strictly ordered")
            previous = key


@dataclass(frozen=True)
class ParticleEffectResult:
    effect_identifier: str
    emitter_id: int
    metadata: EffectRenderMetadata
    tracks: Tuple[ParticleTrack, ...]


@dataclass(frozen=True)
class CanonicalParticleResult:
    """Complete renderer-independent output of a particle bake."""

    frame_rate: float
    coordinate_system: str
    effects: Tuple[ParticleEffectResult, ...]
    diagnostics: Tuple[Diagnostic, ...] = ()
    schema_version: str = "particle-core-render-result/1"

    def __post_init__(self) -> None:
        if not math.isfinite(self.frame_rate) or self.frame_rate <= 0:
            raise ValueError("frame_rate must be positive and finite")
        if self.coordinate_system != "bedrock_right_handed_y_up":
            raise ValueError("Canonical results must remain in Bedrock space")

    @property
    def frame_count(self) -> int:
        return 1 + max(
            (
                sample.frame
                for effect in self.effects
                for track in effect.tracks
                for sample in track.samples
            ),
            default=-1,
        )


class RenderSampleProvider(Protocol):
    """Integration adapter from simulation snapshots to evaluated render values."""

    def effect_metadata(self, emitter: EmitterSnapshot) -> EffectRenderMetadata:
        ...

    def particle_appearance(
        self,
        emitter: EmitterSnapshot,
        particle: ParticleSnapshot,
        frame: int,
        time: float,
    ) -> ParticleAppearance:
        ...


@dataclass
class _MutableEffect:
    identifier: str
    metadata: EffectRenderMetadata
    tracks: Dict[int, list] = field(default_factory=dict)


class CanonicalResultBuilder:
    """Deterministic collector suitable for legacy or 3.0 runtime adapters."""

    def __init__(self, frame_rate: float) -> None:
        if not math.isfinite(frame_rate) or frame_rate <= 0:
            raise ValueError("frame_rate must be positive and finite")
        self.frame_rate = float(frame_rate)
        self._effects: Dict[int, _MutableEffect] = {}
        self._diagnostics = []

    def register_effect(
        self,
        emitter_id: int,
        effect_identifier: str,
        metadata: EffectRenderMetadata,
    ) -> None:
        if emitter_id < 1:
            raise ValueError("emitter_id must be positive")
        existing = self._effects.get(emitter_id)
        if existing is not None:
            if (
                existing.identifier != effect_identifier
                or existing.metadata != metadata
            ):
                raise ValueError("Emitter metadata changed after registration")
            return
        self._effects[emitter_id] = _MutableEffect(
            effect_identifier, metadata
        )

    def add_sample(
        self, emitter_id: int, sample: CanonicalParticleSample
    ) -> None:
        try:
            effect = self._effects[emitter_id]
        except KeyError as exc:
            raise KeyError("Register emitter before adding samples") from exc
        samples = effect.tracks.setdefault(sample.particle_id, [])
        if samples and (sample.frame, sample.time) <= (
            samples[-1].frame,
            samples[-1].time,
        ):
            raise ValueError("Samples must be added in chronological order")
        samples.append(sample)

    def ingest_snapshot(
        self,
        snapshot: SimulationSnapshot,
        provider: RenderSampleProvider,
        *,
        frame: Optional[int] = None,
    ) -> None:
        sample_frame = snapshot.tick if frame is None else frame
        for emitter in snapshot.emitters:
            metadata = provider.effect_metadata(emitter)
            self.register_effect(
                emitter.emitter_id, emitter.effect_identifier, metadata
            )
            for particle in emitter.particles:
                appearance = provider.particle_appearance(
                    emitter, particle, sample_frame, snapshot.time
                )
                self.add_sample(
                    emitter.emitter_id,
                    CanonicalParticleSample(
                        particle_id=particle.particle_id,
                        frame=sample_frame,
                        time=snapshot.time,
                        bedrock_position=particle.position,
                        size_half_extents=appearance.size_half_extents,
                        rgba=appearance.rgba,
                        uv_rect=appearance.uv_rect,
                        rotation_degrees=appearance.rotation_degrees,
                        direction=appearance.direction,
                        alive=appearance.alive and particle.alive,
                    ),
                )

    def extend_diagnostics(self, diagnostics: Tuple[Diagnostic, ...]) -> None:
        self._diagnostics.extend(diagnostics)

    def build(self) -> CanonicalParticleResult:
        effects = []
        for emitter_id in sorted(self._effects):
            effect = self._effects[emitter_id]
            tracks = tuple(
                ParticleTrack(particle_id, tuple(effect.tracks[particle_id]))
                for particle_id in sorted(effect.tracks)
            )
            effects.append(
                ParticleEffectResult(
                    effect.identifier, emitter_id, effect.metadata, tracks
                )
            )
        return CanonicalParticleResult(
            self.frame_rate,
            "bedrock_right_handed_y_up",
            tuple(effects),
            tuple(self._diagnostics),
        )

