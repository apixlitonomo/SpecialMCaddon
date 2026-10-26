"""Pure helpers for Bedrock particle direction / orientation (no bpy)."""

import math


def bedrock_direction_to_blender(vec):
    """Match builder.py velocity mapping: Bedrock (x,y,z) -> Blender (x,-z,y)."""
    x, y, z = (float(vec[0]), float(vec[1]), float(vec[2]))
    return (x, -z, y)


def normalize_vec(vec):
    x, y, z = vec
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-8:
        return (0.0, 0.0, 0.0)
    return (x / length, y / length, z / length)


def resolve_emitter_direction(shape_direction, offset, default=(0.0, 0.0, 1.0)):
    """Resolve Bedrock emitter_shape direction like simulator.get_shape_offset_and_dir tail."""
    direction = default
    if shape_direction == "outwards":
        ox, oy, oz = offset
        length = math.sqrt(ox * ox + oy * oy + oz * oz)
        if length > 1e-8:
            direction = (ox / length, oy / length, oz / length)
    elif shape_direction == "inwards":
        ox, oy, oz = offset
        length = math.sqrt(ox * ox + oy * oy + oz * oz)
        if length > 1e-8:
            direction = (-ox / length, -oy / length, -oz / length)
    elif isinstance(shape_direction, (list, tuple)) and len(shape_direction) >= 3:
        direction = normalize_vec(
            (float(shape_direction[0]), float(shape_direction[1]), float(shape_direction[2]))
        )
    return direction


def is_vertical_rain_direction(bedrock_direction, tolerance=0.99):
    """True when Bedrock direction points straight down (Kamino-style rain)."""
    dx, dy, dz = normalize_vec(bedrock_direction)
    return dy < -tolerance and abs(dx) < 1.0 - tolerance and abs(dz) < 1.0 - tolerance
