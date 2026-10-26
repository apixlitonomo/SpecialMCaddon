"""Pure Java model geometry helpers (no bpy dependency).

Face layout, UV corners, face ``rotation`` and auto-UV follow Minecraft's
FaceBakery/FaceInfo exactly, so every model bakes the way the game does.
"""

import math

# Model units a later element grows along its own face normals per earlier
# coplanar face it overlaps (1/64 of a model pixel, 1 mm in Blender).
JAVA_COPLANAR_LIFT = 0.016


def java_to_blender_point(point):
    """Java model space to Blender as a turn, never a mirror image.

    ``(x, y, z) -> ((16 - x), z, y) / 16``: up stays up, the north side (the
    front of blocks and worn items) faces Blender's front view, and east lands
    on the left, where it is when that side is seen in-game. Bedrock and
    ``.bbmodel`` imports turn Blockbench space the same way. Turning about the
    block centre (x = 8) keeps every model where the old mirrored axes put it.
    """
    x, y, z = (float(point[0]), float(point[1]), float(point[2]))
    return ((16.0 - x) / 16.0, z / 16.0, y / 16.0)


def java_to_mirrored_blender_point(point):
    """Schematic world axes, ``(x, y, z) -> (x, z, y) / 16``: a mirror image.

    Litematic and WorldEdit builds keep these axes so existing scenes line up;
    Axiom blueprints are turned afterwards. Faces in this space need mirrored
    loops (``java_blender_face(..., mirrored=True)``).
    """
    x, y, z = (float(point[0]), float(point[1]), float(point[2]))
    return (x / 16.0, z / 16.0, y / 16.0)


def _vec_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vec_normalize(v):
    length = math.sqrt(_vec_dot(v, v))
    if length < 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


def _vec_cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def rotate_point_java(point, angle_deg, axis, origin):
    """Rotate ``point`` in native Java/Minecraft model space (right-handed).

    Must run *before* ``java_to_blender_point``. Applying Java axis names after
    the axis change reverses handedness and detaches rotated parts (guards, loops).
    """
    ox, oy, oz = (float(origin[0]), float(origin[1]), float(origin[2]))
    x, y, z = float(point[0]) - ox, float(point[1]) - oy, float(point[2]) - oz
    t = math.radians(float(angle_deg))
    c, s = math.cos(t), math.sin(t)
    axis = str(axis).lower()
    if axis == "x":
        # +X: y' = y cos - z sin; z' = y sin + z cos
        return (x + ox, y * c - z * s + oy, y * s + z * c + oz)
    if axis == "z":
        # +Z: x' = x cos - y sin; y' = x sin + y cos
        return (x * c - y * s + ox, x * s + y * c + oy, z + oz)
    # +Y (Minecraft): x' = x cos + z sin; z' = -x sin + z cos
    return (x * c + z * s + ox, y + oy, -x * s + z * c + oz)


def element_vertices_java(from_v, to_v):
    """Element corners; index bit order is x, y, z from ``from`` (0) to ``to`` (1).

    ``from``/``to`` are used as authored, like FaceBakery: an element with
    ``from`` > ``to`` on an axis bakes inside out (Blockbench inverted cubes).
    """
    x0, y0, z0 = (float(from_v[0]), float(from_v[1]), float(from_v[2]))
    x1, y1, z1 = (float(to_v[0]), float(to_v[1]), float(to_v[2]))
    return [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]


def _element_rotation_axes(rotation):
    """Ordered ``(axis, angle)`` steps for both rotation schemas.

    Classic models use ``{"axis", "angle"}``. 1.21.11+ models may instead give
    ``{"x", "y", "z"}`` degrees, applied X, then Y, then Z.
    """
    if not isinstance(rotation, dict):
        return []
    if "axis" in rotation or "angle" in rotation:
        try:
            angle = float(rotation.get("angle", 0.0) or 0.0)
        except (TypeError, ValueError):
            return []
        axis = str(rotation.get("axis", "y")).lower()
        return [(axis, angle)] if abs(angle) >= 1e-4 else []
    steps = []
    for axis in ("x", "y", "z"):
        try:
            angle = float(rotation.get(axis, 0.0) or 0.0)
        except (TypeError, ValueError):
            angle = 0.0
        if abs(angle) >= 1e-4:
            steps.append((axis, angle))
    return steps


def apply_element_rotation_java(verts_java, rotation):
    """Apply element ``rotation`` in Java space; verts stay in Java units."""
    steps = _element_rotation_axes(rotation)
    if not steps:
        return verts_java
    origin = rotation.get("origin", [8, 8, 8])
    result = list(verts_java)
    if rotation.get("rescale", False) and len(steps) == 1:
        axis, angle = steps[0]
        scale_factor = 1.0 / math.cos(math.radians(abs(angle)))
        ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])
        scaled = []
        for v in result:
            x, y, z = v[0] - ox, v[1] - oy, v[2] - oz
            # Scaling both in-plane axes equally commutes with the rotation,
            # so rescaling first matches FaceBakery's rotate-then-scale.
            if axis == "x":
                y, z = y * scale_factor, z * scale_factor
            elif axis == "y":
                x, z = x * scale_factor, z * scale_factor
            elif axis == "z":
                x, y = x * scale_factor, y * scale_factor
            scaled.append((x + ox, y + oy, z + oz))
        result = scaled
    for axis, angle in steps:
        result = [rotate_point_java(v, angle, axis, origin) for v in result]
    return result


# Minecraft FaceInfo vertex order per face, as element_vertices_java indices.
# Each loop is wound outward in Java space and stays outward in Blender; only
# the mirrored schematic axes reverse them (see java_blender_face).
JAVA_FACE_INDICES = {
    "down": (4, 0, 1, 5),
    "up": (3, 7, 6, 2),
    "north": (2, 1, 0, 3),
    "south": (7, 4, 5, 6),
    "west": (3, 0, 4, 7),
    "east": (6, 5, 1, 2),
}

# Which model axis each face is perpendicular to.
JAVA_FACE_AXIS = {
    "north": "z", "south": "z",
    "east": "x", "west": "x",
    "up": "y", "down": "y",
}


def _quarter_turns(rotation):
    try:
        return (int(rotation or 0) // 90) % 4
    except (TypeError, ValueError):
        return 0


def rotate_uv_corners(uvs, rotation):
    """Rotate UVs given in JAVA_FACE_INDICES order clockwise by ``rotation``.

    FaceBakery gives loop vertex ``i`` the corner of vertex ``i + turns``.
    """
    turns = _quarter_turns(rotation)
    uvs = list(uvs)
    return uvs[turns:] + uvs[:turns]


def java_face_uv_rect(face, face_name, from_v, to_v):
    """Return a face's ``[u1, v1, u2, v2]`` in 0-16 model UV space.

    Faces without ``uv`` use Minecraft's BlockElement.uvsByFace projection.
    """
    uv = face.get("uv") if isinstance(face, dict) else None
    if isinstance(uv, (list, tuple)) and len(uv) >= 4:
        try:
            return [float(value) for value in uv[:4]]
        except (TypeError, ValueError):
            pass
    x0, y0, z0 = (float(value) for value in from_v[:3])
    x1, y1, z1 = (float(value) for value in to_v[:3])
    return {
        "down": [x0, 16.0 - z1, x1, 16.0 - z0],
        "up": [x0, z0, x1, z1],
        "north": [16.0 - x1, 16.0 - y1, 16.0 - x0, 16.0 - y0],
        "south": [x0, 16.0 - y1, x1, 16.0 - y0],
        "west": [z0, 16.0 - y1, z1, 16.0 - y0],
        "east": [16.0 - z1, 16.0 - y1, 16.0 - z0, 16.0 - y0],
    }.get(face_name, [0.0, 0.0, 16.0, 16.0])


def face_uv_corners(face, face_name, axis=None, from_v=(0, 0, 0), to_v=(16, 16, 16)):
    """Return the four Blender UVs of one face in JAVA_FACE_INDICES order.

    Corner ``c`` of the rectangle is ``(u1|u2, v1|v2)`` with c=0 (u1,v1),
    1 (u1,v2), 2 (u2,v2), 3 (u2,v1); FaceBakery hands loop vertex ``i``
    corner ``(i + rotation/90) % 4``. ``axis`` is kept for older callers.
    """
    u1, v1, u2, v2 = java_face_uv_rect(face, face_name, from_v, to_v)
    turns = _quarter_turns(face.get("rotation") if isinstance(face, dict) else 0)
    corners = []
    for index in range(4):
        corner = (index + turns) % 4
        u = u1 if corner in (0, 1) else u2
        v = v1 if corner in (0, 3) else v2
        corners.append((u / 16.0, 1.0 - v / 16.0))
    return corners


def _matrix_multiply(a, b):
    return [
        [sum(a[row][k] * b[k][column] for k in range(4)) for column in range(4)]
        for row in range(4)
    ]


def _rotation_matrix(axis, degrees):
    c = math.cos(math.radians(degrees))
    s = math.sin(math.radians(degrees))
    # Snap the quarter turns blockstates use so directions stay exact.
    c, s = round(c, 12), round(s, 12)
    if axis == "x":
        return [[1, 0, 0, 0], [0, c, -s, 0], [0, s, c, 0], [0, 0, 0, 1]]
    return [[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [0, 0, 0, 1]]


def _transpose3(matrix):
    result = [row[:] for row in matrix]
    for row in range(3):
        for column in range(3):
            result[row][column] = matrix[column][row]
    return result


def _translation(offset):
    return [[1, 0, 0, offset], [0, 1, 0, offset], [0, 0, 1, offset], [0, 0, 0, 1]]


_FACE_DIRECTION = {
    "down": (0, -1, 0), "up": (0, 1, 0), "north": (0, 0, -1),
    "south": (0, 0, 1), "west": (-1, 0, 0), "east": (1, 0, 0),
}
# BlockMath.VANILLA_UV_TRANSFORM_LOCAL_TO_GLOBAL.
_UV_LOCAL_TO_GLOBAL = {
    "south": ("y", 0.0), "east": ("y", 90.0), "west": ("y", -90.0),
    "north": ("y", 180.0), "up": ("x", -90.0), "down": ("x", 90.0),
}


def variant_rotation_matrix(x_angle, y_angle):
    """BlockModelRotation: rotateYXZ(-y, -x, 0) (X is applied first)."""
    return _matrix_multiply(
        _rotation_matrix("y", -float(y_angle or 0.0)),
        _rotation_matrix("x", -float(x_angle or 0.0)),
    )


def rotate_face_direction(face_name, matrix):
    """Direction.rotate: the face a model rotation turns ``face_name`` into."""
    x, y, z = _FACE_DIRECTION[face_name]
    rotated = tuple(
        matrix[row][0] * x + matrix[row][1] * y + matrix[row][2] * z
        for row in range(3)
    )
    return max(
        _FACE_DIRECTION,
        key=lambda name: _vec_dot(_FACE_DIRECTION[name], rotated),
    )


def uvlock_face(face, face_name, from_v, to_v, x_angle, y_angle):
    """FaceBakery.recomputeUVs for a uvlocked blockstate rotation.

    Returns a face copy whose ``uv`` and ``rotation`` keep the texture
    aligned to the world after the model is rotated by ``x``/``y``.
    """
    rotation = variant_rotation_matrix(x_angle, y_angle)
    target = rotate_face_direction(face_name, rotation)
    local_axis, local_angle = _UV_LOCAL_TO_GLOBAL[face_name]
    global_axis, global_angle = _UV_LOCAL_TO_GLOBAL[target]
    transform = _matrix_multiply(
        _matrix_multiply(
            _rotation_matrix(local_axis, -local_angle),
            _transpose3(rotation),
        ),
        _rotation_matrix(global_axis, global_angle),
    )
    transform = _matrix_multiply(
        _matrix_multiply(_translation(0.5), transform), _translation(-0.5)
    )

    uvs = java_face_uv_rect(face, face_name, from_v, to_v)
    turns = _quarter_turns(face.get("rotation") if isinstance(face, dict) else 0)

    def corner(index):
        reverse = (index + 4 - turns) % 4
        u = uvs[0] if reverse in (0, 1) else uvs[2]
        v = uvs[1] if reverse in (0, 3) else uvs[3]
        return u, v

    def apply(u, v):
        x, y = u / 16.0, v / 16.0
        return (
            16.0 * (transform[0][0] * x + transform[0][1] * y + transform[0][3]),
            16.0 * (transform[1][0] * x + transform[1][1] * y + transform[1][3]),
        )

    def sign(value):
        return (value > 0) - (value < 0)

    f, g = corner(0)
    h, i = apply(f, g)
    j, k = corner(2)
    l, m = apply(j, k)
    u_start, u_end = (h, l) if sign(j - f) == sign(l - h) else (l, h)
    v_start, v_end = (i, m) if sign(k - g) == sign(m - i) else (m, i)
    radians = math.radians(turns * 90.0)
    cos_r, sin_r = math.cos(radians), math.sin(radians)
    direction_x = transform[0][0] * cos_r + transform[0][1] * sin_r
    direction_y = transform[1][0] * cos_r + transform[1][1] * sin_r
    quarter = int(round(math.degrees(math.atan2(direction_y, direction_x)) / 90.0))
    new_rotation = (-quarter * 90) % 360
    locked = dict(face) if isinstance(face, dict) else {}
    locked["uv"] = [u_start, v_start, u_end, v_end]
    locked["rotation"] = new_rotation
    return locked


def java_face_has_area(face_name, from_v, to_v):
    """False for the zero-width sides of flat elements (lines in-game)."""
    normal_axis = {"x": 0, "y": 1, "z": 2}[JAVA_FACE_AXIS[face_name]]
    return all(
        abs(float(to_v[axis]) - float(from_v[axis])) > 1e-9
        for axis in range(3)
        if axis != normal_axis
    )


def java_blender_face(face_name, face, from_v, to_v, mirrored=False):
    """Blender-wound ``(corner indices, uvs)`` for one face of an element.

    ``mirrored`` is for corners from ``java_to_mirrored_blender_point``: a
    mirror image turns every loop inside out, so it runs backwards there.
    """
    indices = JAVA_FACE_INDICES[face_name]
    uvs = face_uv_corners(face, face_name, JAVA_FACE_AXIS[face_name], from_v, to_v)
    if mirrored:
        return tuple(reversed(indices)), list(reversed(uvs))
    return tuple(indices), list(uvs)


def _face_normal(points):
    """Newell normal of a planar polygon (robust for any convex quad)."""
    nx = ny = nz = 0.0
    count = len(points)
    for index in range(count):
        ax, ay, az = points[index]
        bx, by, bz = points[(index + 1) % count]
        nx += (ay - by) * (az + bz)
        ny += (az - bz) * (ax + bx)
        nz += (ax - bx) * (ay + by)
    return _vec_normalize((nx, ny, nz))


def _plane_frame(normal):
    ref = (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)
    tangent_u = _vec_normalize(_vec_cross(ref, normal))
    tangent_v = _vec_normalize(_vec_cross(normal, tangent_u))
    return tangent_u, tangent_v


def element_seen_from_behind(from_v, to_v):
    """Cards (zero thickness) and inside-out cubes show faces from both sides.

    Inverting an odd number of axes mirrors the element inside out; two
    inverted axes are only a half turn and still face outward.
    """
    spans = [float(to_v[axis]) - float(from_v[axis]) for axis in range(3)]
    return any(span == 0.0 for span in spans) or sum(span < 0.0 for span in spans) % 2 == 1


def element_lift_ranks(elements):
    """How many lift steps each element needs to win its coplanar overlaps.

    ``elements`` holds ``(corners, loops, seen_from_behind)`` per element in
    model order: Blender-space corners, Blender-wound face loops, and
    ``element_seen_from_behind``. Minecraft draws coplanar quads in model
    order and the later one wins. A face overlapping an earlier face on the
    same plane needs its element lifted when they face the same way
    (overlays, decals) or sit back to back on a card or inverted cube. Boxes
    that merely touch hide both faces inside the model, so they never lift.
    """
    buckets = {}
    ranks = []
    quantum = 1.0e-4
    for element_index, (corners, loops, sided) in enumerate(elements):
        rank = 0
        for loop in loops:
            points = [corners[index] for index in loop]
            normal = _face_normal(points)
            if normal == (0.0, 0.0, 0.0):
                continue
            facing = 1
            for component in normal:
                if abs(component) > 1e-6:
                    facing = 1 if component > 0.0 else -1
                    break
            plane_normal = tuple(round(component * facing, 3) for component in normal)
            level = int(round(_vec_dot(normal, points[0]) * facing / quantum))
            tangent_u, tangent_v = _plane_frame(plane_normal)
            us = [_vec_dot(point, tangent_u) for point in points]
            vs = [_vec_dot(point, tangent_v) for point in points]
            bounds = (min(us), max(us), min(vs), max(vs))
            overlaps = 0
            for neighbour in (level - 1, level, level + 1):
                for other_bounds, other_facing, other_sided in buckets.get(
                    (plane_normal, neighbour), ()
                ):
                    if not (
                        min(bounds[1], other_bounds[1]) - max(bounds[0], other_bounds[0]) > 1e-6
                        and min(bounds[3], other_bounds[3]) - max(bounds[2], other_bounds[2]) > 1e-6
                    ):
                        continue
                    if other_facing == facing or sided or other_sided:
                        overlaps += 1
            rank = max(rank, overlaps)
            buckets.setdefault((plane_normal, level), []).append((bounds, facing, sided))
        ranks.append(rank)
    return ranks


def lift_element_bounds(from_v, to_v, amount):
    """Move every face of an element ``amount`` model units along its normal.

    The element stays one closed box (no cracks between faces). Faces of a
    normal element move outward; an inverted axis mirrors the faces across
    it, which flips the normal of every face lying along that axis.
    """
    from_v = [float(value) for value in from_v[:3]]
    to_v = [float(value) for value in to_v[:3]]
    inverted = [from_v[axis] > to_v[axis] for axis in range(3)]
    lifted_from, lifted_to = list(from_v), list(to_v)
    for axis in range(3):
        flips = sum(inverted[other] for other in range(3) if other != axis) % 2
        step = amount if flips else -amount
        lifted_from[axis] += step
        lifted_to[axis] -= step
    return lifted_from, lifted_to


def build_extruded_sprite_mesh(alpha_grid, width=1.0, height=1.0, depth=0.0625):
    """Build Minecraft-style pixel extrusion from a top-down alpha mask.

    Vertices are intentionally face-local. This keeps UV seams exact and avoids
    the coincident front/back planes that caused Z-fighting on generated items.
    """
    rows = len(alpha_grid) if isinstance(alpha_grid, (list, tuple)) else 0
    columns = len(alpha_grid[0]) if rows and isinstance(alpha_grid[0], (list, tuple)) else 0
    if rows <= 0 or columns <= 0:
        return [], [], []
    mask = [
        [bool(row[column]) if isinstance(row, (list, tuple)) and column < len(row) else False for column in range(columns)]
        for row in alpha_grid
    ]
    verts = []
    faces = []
    loop_uvs = []
    pixel_x = float(width) / columns
    pixel_z = float(height) / rows
    front_y = -float(depth) / 2.0
    back_y = float(depth) / 2.0

    def emit(corners, uvs):
        base = len(verts)
        verts.extend(corners)
        faces.append((base, base + 1, base + 2, base + 3))
        loop_uvs.append(uvs)

    def opaque(row, column):
        return 0 <= row < rows and 0 <= column < columns and mask[row][column]

    for row in range(rows):
        for column in range(columns):
            if not mask[row][column]:
                continue
            x0 = -float(width) / 2.0 + column * pixel_x
            x1 = x0 + pixel_x
            z1 = float(height) - row * pixel_z
            z0 = z1 - pixel_z
            u0, u1 = column / columns, (column + 1) / columns
            v0, v1 = 1.0 - (row + 1) / rows, 1.0 - row / rows
            cell_uv = [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]

            emit(
                [(x0, front_y, z0), (x1, front_y, z0), (x1, front_y, z1), (x0, front_y, z1)],
                cell_uv,
            )
            emit(
                [(x0, back_y, z1), (x1, back_y, z1), (x1, back_y, z0), (x0, back_y, z0)],
                [(u0, v1), (u1, v1), (u1, v0), (u0, v0)],
            )
            if not opaque(row, column - 1):
                emit(
                    [(x0, back_y, z0), (x0, front_y, z0), (x0, front_y, z1), (x0, back_y, z1)],
                    [(u0, v0), (u1, v0), (u1, v1), (u0, v1)],
                )
            if not opaque(row, column + 1):
                emit(
                    [(x1, front_y, z0), (x1, back_y, z0), (x1, back_y, z1), (x1, front_y, z1)],
                    [(u0, v0), (u1, v0), (u1, v1), (u0, v1)],
                )
            if not opaque(row - 1, column):
                emit(
                    [(x0, front_y, z1), (x1, front_y, z1), (x1, back_y, z1), (x0, back_y, z1)],
                    [(u0, v0), (u1, v0), (u1, v1), (u0, v1)],
                )
            if not opaque(row + 1, column):
                emit(
                    [(x0, back_y, z0), (x1, back_y, z0), (x1, front_y, z0), (x0, front_y, z0)],
                    [(u0, v0), (u1, v0), (u1, v1), (u0, v1)],
                )
    return verts, faces, loop_uvs
