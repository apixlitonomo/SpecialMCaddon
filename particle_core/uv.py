"""Texture-grid and flipbook policy for Bedrock particle billboards."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, Tuple

from .diagnostics import Diagnostic, DiagnosticBag, Severity, SourceRef


class DimensionSource(str, Enum):
    DECLARED = "declared"
    IMAGE = "image"
    DEFAULT = "default"


@dataclass(frozen=True)
class TextureDimensions:
    width: int
    height: int
    width_source: DimensionSource
    height_source: DimensionSource


@dataclass(frozen=True)
class DimensionResult:
    dimensions: TextureDimensions
    diagnostics: Tuple[Diagnostic, ...]


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        return None
    return int(number)


def resolve_texture_dimensions(
    declared_width: Any = None,
    declared_height: Any = None,
    image_size: Optional[Tuple[Any, Any]] = None,
    *,
    source: SourceRef = SourceRef(),
) -> DimensionResult:
    """Resolve each grid axis, defaulting omitted Bedrock dimensions to 1.

    A valid declaration is the Bedrock coordinate grid and remains authoritative
    even when it intentionally differs from the physical image dimensions.
    Invalid declarations fall back to the image dimension when available.
    """

    bag = DiagnosticBag()
    image_width = _positive_int(image_size[0]) if image_size else None
    image_height = _positive_int(image_size[1]) if image_size else None
    declared_values = (declared_width, declared_height)
    image_values = (image_width, image_height)
    labels = ("width", "height")
    resolved = []
    sources = []
    for declared, image_value, label in zip(
        declared_values, image_values, labels
    ):
        declared_value = _positive_int(declared)
        if declared is not None and declared_value is None:
            bag.add(
                "particle.uv.invalid_texture_%s" % label,
                "Declared texture_%s must be a positive integer" % label,
                Severity.WARNING,
                source.child("texture_" + label),
                "The loaded image dimension will be used when available.",
            )
        if declared_value is not None:
            resolved.append(declared_value)
            sources.append(DimensionSource.DECLARED)
            if image_value is not None and image_value != declared_value:
                bag.add(
                    "particle.uv.declared_image_mismatch",
                    (
                        "Declared texture_%s grid %d differs from image %d; "
                        "the declared Bedrock coordinate grid is authoritative"
                        % (label, declared_value, image_value)
                    ),
                    Severity.INFO,
                    source.child("texture_" + label),
                )
        elif declared is not None and image_value is not None:
            resolved.append(image_value)
            sources.append(DimensionSource.IMAGE)
        else:
            resolved.append(1)
            sources.append(DimensionSource.DEFAULT)
            bag.add(
                "particle.uv.default_texture_%s" % label,
                "Using the default Bedrock texture_%s grid of 1" % label,
                Severity.INFO,
                source.child("texture_" + label),
            )
    return DimensionResult(
        TextureDimensions(
            resolved[0], resolved[1], sources[0], sources[1]
        ),
        bag.snapshot(),
    )


def _pair(value: Any, label: str, default: Tuple[float, float]) -> Tuple[float, float]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) >= 2:
            try:
                result = (float(value[0]), float(value[1]))
            except (TypeError, ValueError):
                result = default
            if all(math.isfinite(number) for number in result):
                return result
    return default


@dataclass(frozen=True)
class FlipbookSpec:
    base_uv: Tuple[float, float] = (0.0, 0.0)
    size_uv: Tuple[float, float] = (1.0, 1.0)
    step_uv: Tuple[float, float] = (0.0, 0.0)
    frames_per_second: float = 1.0
    max_frame: float = 1.0
    stretch_to_lifetime: bool = False
    loop: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FlipbookSpec":
        try:
            frames_per_second = float(value.get("frames_per_second", 1.0))
        except (TypeError, ValueError):
            frames_per_second = 1.0
        try:
            max_frame = float(value.get("max_frame", 1.0))
        except (TypeError, ValueError):
            max_frame = 1.0
        if not math.isfinite(frames_per_second):
            frames_per_second = 1.0
        if not math.isfinite(max_frame):
            max_frame = 1.0
        return cls(
            base_uv=_pair(value.get("base_UV"), "base_UV", (0.0, 0.0)),
            size_uv=_pair(value.get("size_UV"), "size_UV", (1.0, 1.0)),
            step_uv=_pair(value.get("step_UV"), "step_UV", (0.0, 0.0)),
            frames_per_second=frames_per_second,
            max_frame=max_frame,
            stretch_to_lifetime=bool(value.get("stretch_to_lifetime", False)),
            loop=bool(value.get("loop", False)),
        )

    @property
    def frame_count(self) -> int:
        # Bedrock calls this max_frame, but documents frame 1 as the first frame.
        return max(1, int(math.floor(self.max_frame)))

    def frame_index(self, age: float, lifetime: Optional[float] = None) -> int:
        age_value = max(0.0, float(age))
        count = self.frame_count
        if self.stretch_to_lifetime and lifetime is not None and lifetime > 0:
            progress = min(1.0, age_value / float(lifetime))
            raw_index = int(math.floor(progress * count))
        else:
            raw_index = int(math.floor(age_value * self.frames_per_second))
        raw_index = max(0, raw_index)
        if self.loop:
            return raw_index % count
        return min(raw_index, count - 1)

    def pixel_rect(
        self, age: float, lifetime: Optional[float] = None
    ) -> Tuple[float, float, float, float]:
        frame = self.frame_index(age, lifetime)
        return (
            self.base_uv[0] + self.step_uv[0] * frame,
            self.base_uv[1] + self.step_uv[1] * frame,
            self.size_uv[0],
            self.size_uv[1],
        )


@dataclass(frozen=True)
class UVFrame:
    frame_index: int
    grid_rect: Tuple[float, float, float, float]
    normalized_rect: Tuple[float, float, float, float]
    dimensions: TextureDimensions


def resolve_uv_frame(
    uv: Mapping[str, Any],
    *,
    age: float,
    lifetime: Optional[float] = None,
    image_size: Optional[Tuple[Any, Any]] = None,
    source: SourceRef = SourceRef(),
) -> Tuple[UVFrame, Tuple[Diagnostic, ...]]:
    dimensions_result = resolve_texture_dimensions(
        uv.get("texture_width"),
        uv.get("texture_height"),
        image_size,
        source=source,
    )
    dimensions = dimensions_result.dimensions
    flipbook_raw = uv.get("flipbook")
    if isinstance(flipbook_raw, Mapping):
        flipbook = FlipbookSpec.from_mapping(flipbook_raw)
        index = flipbook.frame_index(age, lifetime)
        rect = flipbook.pixel_rect(age, lifetime)
    else:
        index = 0
        base = _pair(uv.get("uv"), "uv", (0.0, 0.0))
        size = _pair(uv.get("uv_size"), "uv_size", (1.0, 1.0))
        rect = (base[0], base[1], size[0], size[1])
    normalized = (
        rect[0] / dimensions.width,
        rect[1] / dimensions.height,
        rect[2] / dimensions.width,
        rect[3] / dimensions.height,
    )
    return (
        UVFrame(index, rect, normalized, dimensions),
        dimensions_result.diagnostics,
    )

