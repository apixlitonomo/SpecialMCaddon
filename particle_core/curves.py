"""Bedrock particle curve evaluation.

Semantics follow Microsoft Learn's "Curves in Particle Effects" reference:
linear and Catmull-Rom nodes are evenly spaced, Bezier uses four controls, and
Bezier-chain node keys are input positions with side-specific values/slopes.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Tuple

from .ir import CurveIR, FrozenDict


class CurveEvaluationError(ValueError):
    pass


ValueResolver = Callable[[Any], float]


def numeric_resolver(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        result = float(value)
        if math.isfinite(result):
            return result
    raise CurveEvaluationError("Curve value is not a finite number: %r" % (value,))


def _resolve(value: Any, resolver: ValueResolver) -> float:
    result = float(resolver(value))
    if not math.isfinite(result):
        raise CurveEvaluationError("Curve resolver returned a non-finite value")
    return result


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _cubic_bezier(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    inverse = 1.0 - t
    return (
        inverse * inverse * inverse * p0
        + 3.0 * inverse * inverse * t * p1
        + 3.0 * inverse * t * t * p2
        + t * t * t * p3
    )


def evaluate_linear(nodes: Sequence[Any], x: float, resolver=numeric_resolver) -> float:
    values = tuple(_resolve(node, resolver) for node in nodes)
    if not values:
        raise CurveEvaluationError("linear curve requires at least one node")
    if len(values) == 1:
        return values[0]
    scaled = _clamp01(float(x)) * (len(values) - 1)
    left = min(int(math.floor(scaled)), len(values) - 2)
    amount = scaled - left
    return values[left] + (values[left + 1] - values[left]) * amount


def evaluate_bezier(nodes: Sequence[Any], x: float, resolver=numeric_resolver) -> float:
    if len(nodes) < 4:
        raise CurveEvaluationError("bezier curve requires four nodes")
    controls = tuple(_resolve(node, resolver) for node in nodes[:4])
    return _cubic_bezier(*controls, _clamp01(float(x)))


def evaluate_catmull_rom(
    nodes: Sequence[Any], x: float, resolver=numeric_resolver
) -> float:
    """Evaluate the documented uniform Catmull-Rom form.

    The first and last values are tangent controls.  The visible curve therefore
    starts at node 1 and ends at node -2.
    """

    values = tuple(_resolve(node, resolver) for node in nodes)
    if len(values) < 4:
        raise CurveEvaluationError("catmull_rom curve requires at least four nodes")
    segment_count = len(values) - 3
    scaled = _clamp01(float(x)) * segment_count
    segment = min(int(math.floor(scaled)), segment_count - 1)
    t = scaled - segment
    p0, p1, p2, p3 = values[segment : segment + 4]
    return 0.5 * (
        (2.0 * p1)
        + (-p0 + p2) * t
        + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t * t
        + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t * t * t
    )


@dataclass(frozen=True)
class BezierChainNode:
    x: float
    value: Any = 0.0
    left_value: Any = None
    right_value: Any = None
    slope: Any = None
    left_slope: Any = None
    right_slope: Any = None

    def side_value(self, side: str, resolver: ValueResolver) -> float:
        candidate = self.left_value if side == "left" else self.right_value
        return _resolve(self.value if candidate is None else candidate, resolver)

    def side_slope(self, side: str, resolver: ValueResolver) -> float:
        candidate = self.left_slope if side == "left" else self.right_slope
        if candidate is None:
            candidate = self.slope
        return 0.0 if candidate is None else _resolve(candidate, resolver)


def parse_bezier_chain_nodes(nodes: Mapping[str, Any]) -> Tuple[BezierChainNode, ...]:
    parsed = []
    used_positions = set()
    for raw_x, raw_node in nodes.items():
        try:
            x = float(raw_x)
        except (TypeError, ValueError) as exc:
            raise CurveEvaluationError(
                "Bezier-chain node key is not numeric: %r" % raw_x
            ) from exc
        if not math.isfinite(x):
            raise CurveEvaluationError("Bezier-chain node key must be finite")
        if x in used_positions:
            raise CurveEvaluationError(
                "Bezier-chain contains duplicate numeric key: %s" % x
            )
        used_positions.add(x)
        if isinstance(raw_node, FrozenDict):
            raw_node = dict(raw_node.items())
        if not isinstance(raw_node, Mapping):
            raw_node = {"value": raw_node}
        parsed.append(
            BezierChainNode(
                x=x,
                value=raw_node.get("value", 0.0),
                left_value=raw_node.get("left_value"),
                right_value=raw_node.get("right_value"),
                slope=raw_node.get("slope"),
                left_slope=raw_node.get("left_slope"),
                right_slope=raw_node.get("right_slope"),
            )
        )
    parsed.sort(key=lambda node: node.x)
    if len(parsed) < 2:
        raise CurveEvaluationError("bezier_chain requires at least two nodes")
    return tuple(parsed)


def evaluate_bezier_chain(
    nodes: Mapping[str, Any],
    x: float,
    resolver: ValueResolver = numeric_resolver,
) -> float:
    chain = parse_bezier_chain_nodes(nodes)
    x_value = float(x)
    if not math.isfinite(x_value):
        raise CurveEvaluationError("Bezier-chain input must be finite")
    if x_value <= chain[0].x:
        return chain[0].side_value("left", resolver)
    if x_value >= chain[-1].x:
        return chain[-1].side_value("right", resolver)

    positions = [node.x for node in chain]
    right_index = bisect.bisect_right(positions, x_value)
    left = chain[right_index - 1]
    right = chain[right_index]
    width = right.x - left.x
    if width <= 0.0:
        raise CurveEvaluationError("Bezier-chain node positions must increase")
    t = (x_value - left.x) / width
    p0 = left.side_value("right", resolver)
    p3 = right.side_value("left", resolver)
    p1 = p0 + left.side_slope("right", resolver) * width / 3.0
    p2 = p3 - right.side_slope("left", resolver) * width / 3.0
    return _cubic_bezier(p0, p1, p2, p3, t)


def evaluate_curve(
    curve_type: str,
    nodes: Any,
    input_value: float,
    horizontal_range: float = 1.0,
    resolver: ValueResolver = numeric_resolver,
) -> float:
    """Evaluate one curve after the caller has evaluated its input expression."""

    if curve_type == "bezier_chain":
        if isinstance(nodes, FrozenDict):
            nodes = dict(nodes.items())
        if not isinstance(nodes, Mapping):
            raise CurveEvaluationError("bezier_chain nodes must be a mapping")
        return evaluate_bezier_chain(nodes, input_value, resolver)

    if isinstance(nodes, (str, bytes)) or not isinstance(nodes, Sequence):
        raise CurveEvaluationError("%s nodes must be a sequence" % curve_type)
    range_value = _resolve(horizontal_range, resolver)
    if range_value == 0.0:
        raise CurveEvaluationError("horizontal_range must not be zero")
    x = float(input_value) / range_value
    if curve_type == "linear":
        return evaluate_linear(nodes, x, resolver)
    if curve_type == "bezier":
        return evaluate_bezier(nodes, x, resolver)
    if curve_type == "catmull_rom":
        return evaluate_catmull_rom(nodes, x, resolver)
    raise CurveEvaluationError("Unknown curve type: %s" % curve_type)


def evaluate_curve_ir(
    curve: CurveIR,
    input_value: float,
    resolver: ValueResolver = numeric_resolver,
) -> float:
    return evaluate_curve(
        curve.curve_type,
        curve.nodes,
        input_value,
        horizontal_range=_resolve(curve.horizontal_range, resolver),
        resolver=resolver,
    )

