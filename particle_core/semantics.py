"""Pure Bedrock particle component semantics shared by runtime integrations."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple


Vec3 = Tuple[float, float, float]
EvaluateNumber = Callable[[Any], float]


class ShapeRandom(Protocol):
    def random(self) -> float:
        ...

    def uniform(self, minimum: float, maximum: float) -> float:
        ...


def _vector(
    value: Any, evaluate: EvaluateNumber, default: Vec3 = (0.0, 0.0, 0.0)
) -> Vec3:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return default
    return tuple(
        float(evaluate(value[index])) if index < len(value) else default[index]
        for index in range(3)
    )  # type: ignore[return-value]


def _add(left: Vec3, right: Vec3) -> Vec3:
    return tuple(left[index] + right[index] for index in range(3))  # type: ignore[return-value]


def _subtract(left: Vec3, right: Vec3) -> Vec3:
    return tuple(left[index] - right[index] for index in range(3))  # type: ignore[return-value]


def _multiply(vector: Vec3, scalar: float) -> Vec3:
    return tuple(component * scalar for component in vector)  # type: ignore[return-value]


def _length(vector: Vec3) -> float:
    return math.sqrt(sum(component * component for component in vector))


def _normalize(vector: Vec3) -> Vec3:
    length = _length(vector)
    if length <= 1.0e-12:
        return (0.0, 0.0, 0.0)
    return _multiply(vector, 1.0 / length)


def _cross(left: Vec3, right: Vec3) -> Vec3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


@dataclass(frozen=True)
class EntityAABB:
    minimum: Vec3
    maximum: Vec3

    def __post_init__(self) -> None:
        if any(
            not math.isfinite(component)
            for component in self.minimum + self.maximum
        ):
            raise ValueError("Entity AABB values must be finite")
        if any(
            self.minimum[index] > self.maximum[index] for index in range(3)
        ):
            raise ValueError("Entity AABB minimum must not exceed maximum")

    @property
    def center(self) -> Vec3:
        return tuple(
            (self.minimum[index] + self.maximum[index]) * 0.5
            for index in range(3)
        )  # type: ignore[return-value]

    @property
    def half_dimensions(self) -> Vec3:
        return tuple(
            (self.maximum[index] - self.minimum[index]) * 0.5
            for index in range(3)
        )  # type: ignore[return-value]

    @classmethod
    def from_value(cls, value: Any) -> Optional["EntityAABB"]:
        def vec3(candidate):
            if (
                not isinstance(candidate, Sequence)
                or isinstance(candidate, (str, bytes))
                or len(candidate) != 3
            ):
                raise ValueError("Expected three AABB coordinates")
            return tuple(float(item) for item in candidate)

        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            minimum = value.get("minimum", value.get("min"))
            maximum = value.get("maximum", value.get("max"))
            if minimum is not None and maximum is not None:
                try:
                    return cls(vec3(minimum), vec3(maximum))
                except (TypeError, ValueError):
                    return None
            center = value.get("center")
            dimensions = value.get("dimensions", value.get("size"))
            if center is not None and dimensions is not None:
                try:
                    center_value = vec3(center)
                    half = tuple(item * 0.5 for item in vec3(dimensions))
                    return cls(
                        _subtract(center_value, half),
                        _add(center_value, half),
                    )
                except (TypeError, ValueError):
                    return None
        if isinstance(value, Sequence) and len(value) == 2:
            try:
                return cls(vec3(value[0]), vec3(value[1]))
            except (TypeError, ValueError):
                return None
        return None


@dataclass(frozen=True)
class ShapeSample:
    position: Vec3
    direction: Vec3


def _sample_box_point(
    half: Vec3, surface_only: bool, random: ShapeRandom
) -> Vec3:
    hx, hy, hz = (max(0.0, abs(value)) for value in half)
    if not surface_only:
        return (
            random.uniform(-hx, hx),
            random.uniform(-hy, hy),
            random.uniform(-hz, hz),
        )

    # Weight face pairs by area so rectangular boxes do not over-sample their
    # narrow faces. Degenerate boxes naturally reduce to a plane or line.
    face_pairs = (
        (hy * hz, 0, hx),
        (hx * hz, 1, hy),
        (hx * hy, 2, hz),
    )
    total = sum(area for area, _axis, _extent in face_pairs)
    if total <= 1.0e-12:
        nonzero = [index for index, extent in enumerate((hx, hy, hz)) if extent > 0]
        point = [0.0, 0.0, 0.0]
        for axis in nonzero:
            extent = (hx, hy, hz)[axis]
            point[axis] = random.uniform(-extent, extent)
        return tuple(point)  # type: ignore[return-value]
    roll = random.random() * total
    selected_axis = 2
    selected_extent = hz
    cumulative = 0.0
    for area, axis, extent in face_pairs:
        cumulative += area
        if roll < cumulative:
            selected_axis, selected_extent = axis, extent
            break
    extents = (hx, hy, hz)
    point = [
        random.uniform(-extents[axis], extents[axis]) for axis in range(3)
    ]
    point[selected_axis] = selected_extent * (
        -1.0 if random.random() < 0.5 else 1.0
    )
    return tuple(point)  # type: ignore[return-value]


def _direction(
    direction_value: Any,
    radial: Vec3,
    evaluate: EvaluateNumber,
    *,
    default: str = "outwards",
) -> Vec3:
    if direction_value is None:
        direction_value = default
    if isinstance(direction_value, str):
        if direction_value.lower() == "inwards":
            return _multiply(_normalize(radial), -1.0)
        if direction_value.lower() == "outwards":
            return _normalize(radial)
        return (0.0, 0.0, 0.0)
    return _normalize(_vector(direction_value, evaluate))


def sample_emitter_shape(
    shape_type: str,
    data: Mapping[str, Any],
    evaluate: EvaluateNumber,
    random: ShapeRandom,
    *,
    entity_aabb: Optional[EntityAABB] = None,
) -> ShapeSample:
    """Sample all documented emitter shapes in Bedrock coordinates."""

    kind = str(shape_type).lower().replace("minecraft:emitter_shape_", "")
    base_offset = _vector(data.get("offset", (0.0, 0.0, 0.0)), evaluate)
    local = (0.0, 0.0, 0.0)
    default_direction = "outwards"

    if kind == "point":
        default_direction = ""
    elif kind == "sphere":
        radius = max(0.0, float(evaluate(data.get("radius", 1.0))))
        z = random.uniform(-1.0, 1.0)
        angle = random.uniform(0.0, math.tau)
        planar = math.sqrt(max(0.0, 1.0 - z * z))
        unit = (planar * math.cos(angle), z, planar * math.sin(angle))
        radial_scale = radius
        if not bool(data.get("surface_only", False)):
            radial_scale *= random.random() ** (1.0 / 3.0)
        local = _multiply(unit, radial_scale)
    elif kind == "box":
        half = _vector(data.get("half_dimensions", (0.5, 0.5, 0.5)), evaluate)
        local = _sample_box_point(
            half, bool(data.get("surface_only", False)), random
        )
    elif kind == "disc":
        normal_value = data.get("plane_normal", (0.0, 1.0, 0.0))
        if isinstance(normal_value, str):
            normal_value = {
                "x": (1.0, 0.0, 0.0),
                "y": (0.0, 1.0, 0.0),
                "z": (0.0, 0.0, 1.0),
            }.get(normal_value.lower(), (0.0, 1.0, 0.0))
        normal = _normalize(_vector(normal_value, evaluate, (0.0, 1.0, 0.0)))
        if _length(normal) <= 1.0e-12:
            normal = (0.0, 1.0, 0.0)
        helper = (0.0, 0.0, 1.0) if abs(normal[2]) < 0.99 else (1.0, 0.0, 0.0)
        first_axis = _normalize(_cross(helper, normal))
        second_axis = _normalize(_cross(normal, first_axis))
        radius = max(0.0, float(evaluate(data.get("radius", 1.0))))
        sampled_radius = radius
        if not bool(data.get("surface_only", False)):
            sampled_radius *= math.sqrt(random.random())
        angle = random.uniform(0.0, math.tau)
        local = _add(
            _multiply(first_axis, sampled_radius * math.cos(angle)),
            _multiply(second_axis, sampled_radius * math.sin(angle)),
        )
    elif kind == "custom":
        default_direction = ""
    elif kind == "entity_aabb":
        if entity_aabb is None:
            # The reference explicitly defines the emitter point as the no-entity
            # fallback, so this is not an invented box.
            local = (0.0, 0.0, 0.0)
        else:
            centered = _sample_box_point(
                entity_aabb.half_dimensions,
                bool(data.get("surface_only", False)),
                random,
            )
            local = _add(entity_aabb.center, centered)
            base_offset = (0.0, 0.0, 0.0)
            radial = centered
            return ShapeSample(
                local,
                _direction(data.get("direction"), radial, evaluate),
            )

    return ShapeSample(
        _add(base_offset, local),
        _direction(
            data.get("direction"),
            local,
            evaluate,
            default=default_direction,
        ),
    )


@dataclass(frozen=True)
class LocalSpacePolicy:
    position: bool = False
    rotation: bool = False
    velocity: bool = False
    invalid_rotation_without_position: bool = False

    @classmethod
    def from_component(cls, value: Any) -> "LocalSpacePolicy":
        if not isinstance(value, Mapping):
            return cls()
        position = bool(value.get("position", False))
        requested_rotation = bool(value.get("rotation", False))
        invalid = requested_rotation and not position
        return cls(
            position=position,
            rotation=requested_rotation and position,
            velocity=bool(value.get("velocity", False)),
            invalid_rotation_without_position=invalid,
        )


def normalize_event_names(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(str(item) for item in value if isinstance(item, str) and item)
    return ()


@dataclass
class EmitterEventScheduler:
    """State machine for documented emitter lifetime event cadence."""

    component: Mapping[str, Any]
    loop_index: int = 0
    creation_fired: bool = False
    expiration_fired: bool = False
    fired_timeline: set = field(default_factory=set)
    fired_travel: set = field(default_factory=set)
    looping_distance_remainders: Dict[int, float] = field(default_factory=dict)

    def creation_events(self) -> Tuple[str, ...]:
        if self.creation_fired:
            return ()
        self.creation_fired = True
        return normalize_event_names(self.component.get("creation_event"))

    def expiration_events(self) -> Tuple[str, ...]:
        if self.expiration_fired:
            return ()
        self.expiration_fired = True
        return normalize_event_names(self.component.get("expiration_event"))

    def begin_loop(self, loop_index: int) -> None:
        self.loop_index = int(loop_index)
        self.fired_timeline.clear()

    def timeline_events(
        self, previous_age: float, age: float
    ) -> Tuple[str, ...]:
        timeline = self.component.get("timeline", {})
        if not isinstance(timeline, Mapping):
            return ()
        result: List[str] = []
        for raw_time, names in sorted(
            timeline.items(), key=lambda pair: _safe_float(pair[0], math.inf)
        ):
            threshold = _safe_float(raw_time, math.inf)
            key = (self.loop_index, str(raw_time))
            if (
                math.isfinite(threshold)
                and key not in self.fired_timeline
                and previous_age <= threshold <= age
            ):
                self.fired_timeline.add(key)
                result.extend(normalize_event_names(names))
        return tuple(result)

    def travel_events(
        self, total_distance: float, delta_distance: float
    ) -> Tuple[str, ...]:
        result: List[str] = []
        travel = self.component.get("travel_distance_events", {})
        if isinstance(travel, Mapping):
            for raw_distance, names in sorted(
                travel.items(), key=lambda pair: _safe_float(pair[0], math.inf)
            ):
                threshold = _safe_float(raw_distance, math.inf)
                key = str(raw_distance)
                if (
                    math.isfinite(threshold)
                    and threshold >= 0.0
                    and key not in self.fired_travel
                    and total_distance >= threshold
                ):
                    self.fired_travel.add(key)
                    result.extend(normalize_event_names(names))

        looping = self.component.get("looping_travel_distance_events", ())
        if isinstance(looping, Sequence) and not isinstance(looping, (str, bytes)):
            for index, item in enumerate(looping):
                if not isinstance(item, Mapping):
                    continue
                interval = _safe_float(item.get("distance"), 0.0)
                if interval <= 0.0:
                    continue
                remainder = self.looping_distance_remainders.get(index, 0.0)
                remainder += max(0.0, float(delta_distance))
                fire_count = int(math.floor(remainder / interval))
                remainder -= fire_count * interval
                self.looping_distance_remainders[index] = remainder
                names = item.get("effects", item.get("effect"))
                for _ in range(fire_count):
                    result.extend(normalize_event_names(names))
        return tuple(result)


def _safe_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default

