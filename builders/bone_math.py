"""Pure-Python Bedrock bone/cube transform helpers (no Blender dependency)."""

from __future__ import annotations

import math


def coerce_float(value, default=0.0):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
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
def bone_rotation(bone_def):
    """Return the authored hierarchical bone rotation."""
    if not isinstance(bone_def, dict):
        return (0.0, 0.0, 0.0)
    return coerce_vector(bone_def.get("rotation", [0, 0, 0]))


def bone_bind_pose_rotation(bone_def):
    """Return the legacy mesh-only bind-pose rotation."""
    if not isinstance(bone_def, dict):
        return (0.0, 0.0, 0.0)
    return coerce_vector(bone_def.get("bind_pose_rotation", [0, 0, 0]))



# Bedrock model units per Blender unit.
UNITS_PER_BLOCK = 16.0


def bedrock_to_blender(point):
    x, y, z = coerce_vector(point)
    return (x / UNITS_PER_BLOCK, z / UNITS_PER_BLOCK, y / UNITS_PER_BLOCK)


def bedrock_rotation_to_euler_components(rotation):
    """Blockbench-equivalent Blender ``XZY`` Euler components."""
    x, y, z = coerce_vector(rotation)
    return (math.radians(x), math.radians(z), math.radians(-y))


def bedrock_rotation_matrix(rotation):
    """Return Blockbench's Bedrock group rotation in Blender coordinates.

    Blockbench mirrors model X, negates imported X/Y angles, and evaluates
    groups in ZYX order. Conjugating that exact transform into the importer's
    Blender (X, Z, Y) coordinates also matches an XZY Euler of ``(x, z, -y)``.
    """
    x, y, z = coerce_vector(rotation)
    if not any(abs(value) > 1e-4 for value in (x, y, z)):
        return _identity4()

    swap_yz = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    source_matrix = bedrock_space_rotation_matrix(rotation)
    return _mul4(swap_yz, _mul4(source_matrix, swap_yz))


def bedrock_space_rotation_matrix(rotation):
    """Return Blockbench-compatible cube rotation in Bedrock model space.

    Blockbench imports Bedrock cube angles as [-x, -y, z], evaluates them in
    ZYX order, and mirrors model X while converting coordinates. Conjugating
    that transform by the X mirror brings the result back to the source
    Bedrock space used by the mesh builder.
    """
    x, y, z = coerce_vector(rotation)
    if not any(abs(value) > 1e-4 for value in (x, y, z)):
        return _identity4()

    rx = math.radians(-x)
    ry = math.radians(-y)
    rz = math.radians(z)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)

    rotate_x = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, cx, -sx, 0.0],
        [0.0, sx, cx, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    rotate_y = [
        [cy, 0.0, sy, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [-sy, 0.0, cy, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    rotate_z = [
        [cz, -sz, 0.0, 0.0],
        [sz, cz, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    mirror_x = [
        [-1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    blockbench_rotation = _mul4(rotate_z, _mul4(rotate_y, rotate_x))
    return _mul4(mirror_x, _mul4(blockbench_rotation, mirror_x))


def _identity4():
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _translation4(tx, ty, tz):
    return [
        [1.0, 0.0, 0.0, tx],
        [0.0, 1.0, 0.0, ty],
        [0.0, 0.0, 1.0, tz],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _mul4(a, b):
    out = [[0.0] * 4 for _ in range(4)]
    for row in range(4):
        for col in range(4):
            out[row][col] = sum(a[row][k] * b[k][col] for k in range(4))
    return out


def _transform_point(matrix, point):
    x, y, z = coerce_vector(point)
    px = matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3]
    py = matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3]
    pz = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3]
    return (px, py, pz)


def _rotate_around_pivot(point, pivot, rotation_matrix):
    pivot = coerce_vector(pivot)
    local = (
        point[0] - pivot[0],
        point[1] - pivot[1],
        point[2] - pivot[2],
    )
    rotated = _transform_point(rotation_matrix, local)
    return (
        rotated[0] + pivot[0],
        rotated[1] + pivot[1],
        rotated[2] + pivot[2],
    )


def bone_point_matrix(bone_def):
    """Bedrock-space matrix rotating geometry around a bone pivot."""
    pivot = bedrock_to_blender(bone_def.get("pivot", [0, 0, 0]))
    rotation = bone_rotation(bone_def)
    if not any(abs(value) > 1e-4 for value in rotation):
        return _identity4()
    rot = bedrock_rotation_matrix(rotation)
    return _mul4(
        _mul4(_translation4(*pivot), rot),
        _translation4(-pivot[0], -pivot[1], -pivot[2]),
    )


def bone_bind_pose_matrix(bone_def):
    """Rotate only this bone's cubes around its pivot (no child inheritance)."""
    pivot = bedrock_to_blender(bone_def.get("pivot", [0, 0, 0]))
    rotation = bone_bind_pose_rotation(bone_def)
    if not any(abs(value) > 1e-4 for value in rotation):
        return _identity4()
    return _mul4(
        _mul4(_translation4(*pivot), bedrock_rotation_matrix(rotation)),
        _translation4(-pivot[0], -pivot[1], -pivot[2]),
    )


def bone_absolute_matrix(bone_name, bones_by_name, cache):
    if bone_name in cache:
        return cache[bone_name]
    bone_def = bones_by_name.get(bone_name)
    if not isinstance(bone_def, dict):
        cache[bone_name] = _identity4()
        return cache[bone_name]

    matrix = bone_point_matrix(bone_def)
    # Seed the cache before recursion so malformed legacy cycles terminate.
    cache[bone_name] = _identity4()
    parent_name = bone_def.get("parent")
    if parent_name != bone_name and parent_name in bones_by_name:
        matrix = _mul4(bone_absolute_matrix(parent_name, bones_by_name, cache), matrix)
    elif parent_name in cache:
        matrix = _mul4(cache[parent_name], matrix)
    cache[bone_name] = matrix
    return matrix


def bone_rest_world_matrix(bone_name, bones_by_name, cache):
    """Return the Blender rest matrix for a Bedrock bone.

    Bedrock pivots are absolute model-space points, not translations relative
    to the parent pivot. Ancestors therefore deform the child's absolute
    pivot, but their pivot translations must never be accumulated into it.
    This is the mesh bind transform without the final negative-pivot
    translation, putting the Blender bone head on the transformed pivot.
    """
    if bone_name in cache:
        return cache[bone_name]
    bone_def = bones_by_name.get(bone_name)
    if not isinstance(bone_def, dict):
        cache[bone_name] = _identity4()
        return cache[bone_name]

    pivot = bedrock_to_blender(bone_def.get("pivot", [0, 0, 0]))
    rotation = bone_rotation(bone_def)
    matrix = _mul4(_translation4(*pivot), bedrock_rotation_matrix(rotation))

    cache[bone_name] = _identity4()
    parent_name = bone_def.get("parent")
    if parent_name != bone_name and parent_name in bones_by_name:
        # A parent rest matrix contains T(parent_pivot) and would incorrectly
        # add that pivot again for every hierarchy level. Use its deformation
        # around the absolute pivot instead.
        parent_deformation = bone_absolute_matrix(parent_name, bones_by_name, {})
        matrix = _mul4(parent_deformation, matrix)
    cache[bone_name] = matrix
    return matrix

def rotate_cube_point_blender(point, cube, bone_def):
    """Rotate a cube corner in native Bedrock space, then convert to Blender.

    Cube ``rotation`` in geometry JSON uses Blockbench ZYX model-space order
    around the cube pivot. Bone/animation rotations use the separate XZY
  convention via ``bedrock_rotation_matrix``.
    """
    bedrock_point = coerce_vector(point)
    cube_rot = coerce_vector(cube.get("rotation", [0, 0, 0]))
    if any(abs(value) > 1e-4 for value in cube_rot):
        if "pivot" in cube:
            pivot = cube["pivot"]
        else:
            # Cube rotation is local to the cube.  When the optional pivot is
            # omitted, Bedrock keeps the cube centered while rotating it.
            # Using the owning bone pivot here drops modern quadruped torsos
            # (pig v3 is a canonical example) toward model-space zero.
            origin = coerce_vector(cube.get("origin", [0, 0, 0]))
            size = coerce_vector(cube.get("size", [0, 0, 0]))
            pivot = tuple(origin[i] + size[i] * 0.5 for i in range(3))
        rot = bedrock_space_rotation_matrix(cube_rot)
        bedrock_point = _rotate_around_pivot(bedrock_point, pivot, rot)
    return bedrock_to_blender(bedrock_point)


def transform_cube_vertex(point, cube, bone_def, bone_name, bones_by_name, bone_cache):
    """Full Bedrock -> Blender bind-pose vertex transform for a cube corner."""
    blender_point = rotate_cube_point_blender(point, cube, bone_def)
    blender_point = _transform_point(bone_bind_pose_matrix(bone_def), blender_point)
    bone_mat = bone_absolute_matrix(bone_name, bones_by_name, bone_cache)
    return _transform_point(bone_mat, blender_point)


def topological_bone_names(bones_data):
    bones_by_name = {
        bone.get("name", "bone"): bone
        for bone in bones_data
        if isinstance(bone, dict)
    }
    ordered = []
    seen = set()
    visiting = set()

    def visit(name):
        if name in seen or name not in bones_by_name:
            return
        parent = bones_by_name[name].get("parent")
        if name in visiting:
            return
        visiting.add(name)
        if isinstance(parent, str) and parent != name and parent in bones_by_name:
            visit(parent)
        if name not in seen:
            seen.add(name)
            ordered.append(name)

        visiting.discard(name)
    for bone in bones_data:
        if isinstance(bone, dict):
            visit(bone.get("name", "bone"))
    return ordered
