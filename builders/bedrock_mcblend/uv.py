"""Shared numeric coercion helpers for the bedrock_mcblend builders.

The UV mapping logic that used to live here was a duplicate of the canonical
mapper in ``builders/bedrock_uv.py``. Geometry building must only go through
``bedrock_uv.build_cube_faces``; this module keeps just the small value
coercion utilities used by geometry and animation code.
"""

MINECRAFT_SCALE_FACTOR = 16.0
EPSILON = 1.0e-6


def coerce_float(value, default=0.0):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                from ...molang import evaluate
                return float(evaluate(value))
            except Exception:
                return default
    return default


def coerce_vector(value, default=(0.0, 0.0, 0.0)):
    if isinstance(value, (int, float, str)):
        return (coerce_float(value),) * 3
    if isinstance(value, (list, tuple)):
        vals = [coerce_float(v) for v in value[:3]]
        while len(vals) < 3:
            vals.append(0.0)
        return tuple(vals)
    return tuple(default)
