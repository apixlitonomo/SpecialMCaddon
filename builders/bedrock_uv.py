"""Pure Bedrock cube UV mapping - no Blender dependency."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import math

FACE_ORDER = ("west", "east", "down", "up", "north", "south")

CUBE_VERTS_TEMPLATE = [
    (0, 0, 0),
    (1, 0, 0),
    (1, 1, 0),
    (0, 1, 0),
    (0, 0, 1),
    (1, 0, 1),
    (1, 1, 1),
    (0, 1, 1),
]

FACE_INDICES = {
    # Bedrock entity-model X points left: the negative/minimum-X side is
    # east (the model's right), while positive/maximum-X is west. Keeping
    # physical sides aligned with their source names is essential for
    # asymmetric box-UV strips and explicit per-face UV dictionaries.
    "east": (0, 4, 7, 3),
    "west": (1, 2, 6, 5),
    "down": (0, 1, 5, 4),
    "up": (3, 7, 6, 2),
    "north": (0, 3, 2, 1),
    "south": (4, 5, 6, 7),
}

EPSILON = 1.0e-6

def blender_winding(indices):
    """Reverse Bedrock-space winding for Blender's swapped-axis convention.

    ``bedrock_to_blender`` maps (x, y, z) -> (x, z, y), a mirror transform,
    so outward faces in Bedrock space become inward in Blender unless the
    loop order is reversed.
    """
    return tuple(reversed(tuple(indices)))


def face_points_normal(points):
    """Unit normal of a planar face given 3+ points as (x, y, z) tuples."""
    if len(points) < 3:
        return (0.0, 0.0, 0.0)
    ax, ay, az = points[0]
    bx, by, bz = points[1]
    cx, cy, cz = points[2]
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length <= EPSILON:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


# Blender units a later cube moves along its own face normals per earlier
# coplanar face it overlaps: 1/64 of a model pixel, the step Java imports use.
COPLANAR_LIFT = 0.016 / 16.0
# Step between exact copies of a cube (0.1 mm): enough to keep Cycles from
# shading one copy in the other's shadow, far below a pixel.
DUPLICATE_LIFT = 1.0e-4


def coplanar_lift_offsets(cubes, lift=COPLANAR_LIFT):
    """Per-point offsets that let later coplanar cubes draw over earlier ones.

    ``cubes`` holds ``(points, loops, is_card, bone)`` per cube in draw order:
    Blender-space points, outward-wound face loops indexing ``points``,
    whether the cube is a collapsed card (drawn from both sides), and the bone
    that carries it. Only cubes on one bone share a plane in every pose:
    separate bones that meet in the bind pose (a blaze's rods, a guardian's
    spikes) are pulled apart by animation and never move. Bedrock
    draws a model's cubes in order with a LessEqual depth test, so where faces
    share a plane the later cube's opaque texels win and its transparent ones
    show the earlier face. Blender has no order for coplanar faces: EEVEE
    paints them black and Cycles skips past them. So a later face that
    overlaps an earlier one on its plane (facing the same way, or back to back
    with a card) moves along its normal by ``lift`` steps: as many as the faces
    it covers, as Java imports do (``java_geometry.element_lift_ranks``), or one
    past the highest of them when that is fewer (a panel over many tiles). A box grows as one
    closed box; a card backed onto an earlier face moves toward that face's
    front, where the game shows it. Boxes that merely touch, face to face
    inside the model, never move. A cube repeated exactly on another bone
    steps out ``DUPLICATE_LIFT`` per earlier copy. Returns ``{point index: (dx, dy, dz)}`` per
    cube, empty for a cube that stays.
    """
    buckets = {}
    duplicates = {}
    quantum = 1.0e-4
    offsets = []
    for points, loops, is_card, bone in cubes:
        faces = []
        count = above = 0
        card_flip = False
        for loop in loops:
            normal = face_points_normal([points[index] for index in loop])
            if normal == (0.0, 0.0, 0.0):
                continue
            facing = 1
            for component in normal:
                if abs(component) > 1.0e-6:
                    facing = 1 if component > 0.0 else -1
                    break
            plane_normal = tuple(round(component * facing, 3) for component in normal)
            corner = points[loop[0]]
            level = int(round(sum(n * c for n, c in zip(normal, corner)) * facing / quantum))
            ref = (1.0, 0.0, 0.0) if abs(plane_normal[0]) < 0.9 else (0.0, 1.0, 0.0)
            tangent_u = _unit(_cross(ref, plane_normal))
            tangent_v = _unit(_cross(plane_normal, tangent_u))
            us = [sum(t * c for t, c in zip(tangent_u, points[index])) for index in loop]
            vs = [sum(t * c for t, c in zip(tangent_v, points[index])) for index in loop]
            bounds = (min(us), max(us), min(vs), max(vs))
            same = opposite = 0
            for neighbour in (level - 1, level, level + 1):
                for other_bounds, other_facing, other_card, other_rank in buckets.get((bone, plane_normal, neighbour), ()):
                    if not (
                        min(bounds[1], other_bounds[1]) - max(bounds[0], other_bounds[0]) > 1.0e-6
                        and min(bounds[3], other_bounds[3]) - max(bounds[2], other_bounds[2]) > 1.0e-6
                    ):
                        continue
                    if other_facing == facing:
                        same += 1
                    elif is_card or other_card:
                        opposite += 1
                    else:
                        continue
                    above = max(above, other_rank + 1)
            count = max(count, same + opposite)
            card_flip = card_flip or (is_card and opposite > same)
            faces.append((loop, normal, (bone, plane_normal, level), (bounds, facing, is_card)))
        # Java counts the faces covered; one step above the highest of them is
        # also enough (a panel over many tiles). Take the smaller: a strict
        # order would make blocks inflated into their neighbours climb a step
        # per block, 40 mm across a block-built statue.
        rank = min(count, above)
        for _loop, _normal, key, entry in faces:
            buckets.setdefault(key, []).append(entry + (rank,))
        # A cube repeated exactly on another bone (a prop copied for each of
        # its animations, a blaze's rods before they spin apart) draws pixel
        # for pixel over its twin in the game. Coincident faces leave Cycles
        # nothing to light, each copy shadowing the other, so every repeat
        # steps out by a hair no render shows.
        shape = tuple(sorted(
            tuple(int(round(c / quantum)) for c in point) for point in points
        ))
        repeats = duplicates.get(shape, 0)
        duplicates[shape] = repeats + 1
        moved = {}
        step = lift * rank * (-1.0 if card_flip else 1.0)
        if not rank and repeats:
            step = DUPLICATE_LIFT * repeats
        if step:
            for loop, normal, _key, _entry in faces:
                for index in loop:
                    current = moved.get(index, (0.0, 0.0, 0.0))
                    moved[index] = tuple(c + n * step for c, n in zip(current, normal))
        offsets.append(moved)
    return offsets


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(vector):
    length = math.sqrt(sum(component * component for component in vector))
    return tuple(component / length for component in vector) if length > EPSILON else vector


def coerce_float(value, default=0.0):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


class UVValidity(str, Enum):
    """How faithfully a UV mapping can be represented by its source schema."""

    EXACT = "EXACT"
    COMPATIBILITY = "COMPATIBILITY"
    VISUAL_ONLY = "VISUAL_ONLY"


@dataclass(frozen=True)
class UVBuildIssue:
    """Structured UV diagnostic produced before Blender data is mutated."""

    code: str
    severity: str
    source_location: str
    message: str
    fallback: str = ""
    validity: UVValidity = UVValidity.EXACT

    def as_dict(self):
        return {
            "code": self.code,
            "severity": self.severity,
            "source_location": self.source_location,
            "message": self.message,
            "fallback": self.fallback,
            "validity": self.validity.value,
        }


class InvalidUVMapping(ValueError):
    """Fatal, source-located Bedrock UV definition error (MCB-E130)."""

    def __init__(self, source_location, message):
        self.issue = UVBuildIssue(
            code="MCB-E130",
            severity="ERROR",
            source_location=str(source_location or "$"),
            message=str(message),
        )
        super().__init__(
            f"{self.issue.code} at {self.issue.source_location}: "
            f"{self.issue.message}"
        )


@dataclass(frozen=True)
class UVMapping:
    """Canonical pixel rectangle plus loop-ordered normalized UV corners."""

    origin: tuple[float, float]
    size: tuple[float, float]
    rotation: float
    ordered_corners: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ]
    schema_version: str
    validity: UVValidity = UVValidity.EXACT


@dataclass
class FaceRecord:
    """One physical polygon with optional authored data for its reverse side."""

    source_location: str
    direction: str
    vertex_indices: tuple[int, int, int, int]
    front_uv: UVMapping
    back_uv: UVMapping | None = None
    front_material: str = ""
    back_material: str = ""
    two_sided: bool = False
    back_source_location: str = ""
    back_direction: str = ""
    pair_mode: str = "none"
    two_sided_reason: str = ""

    @property
    def name(self):
        return self.direction

    @property
    def indices(self):
        return self.vertex_indices

    @property
    def uv_corners(self):
        return self.front_uv.ordered_corners

    @property
    def back_uv_corners(self):
        return self.back_uv.ordered_corners if self.back_uv is not None else None

    @property
    def material_instance(self):
        return self.front_material

    @property
    def back_material_instance(self):
        return self.back_material

    @property
    def back_face_name(self):
        return self.back_direction

    @property
    def validity(self):
        values = [self.front_uv.validity]
        if self.back_uv is not None:
            values.append(self.back_uv.validity)
        if UVValidity.VISUAL_ONLY in values:
            return UVValidity.VISUAL_ONLY
        if UVValidity.COMPATIBILITY in values:
            return UVValidity.COMPATIBILITY
        return UVValidity.EXACT


EmittedFace = FaceRecord


@dataclass
class CubeBuildResult:
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    faces: list[FaceRecord] = field(default_factory=list)
    issues: list[UVBuildIssue] = field(default_factory=list)
    faces_generated: int = 0
    faces_skipped_no_uv: int = 0
    faces_skipped_degenerate: int = 0
    zero_thickness_planes: int = 0
    paired_two_sided_planes: int = 0

    @property
    def validity(self):
        values = [face.validity for face in self.faces]
        if UVValidity.VISUAL_ONLY in values:
            return UVValidity.VISUAL_ONLY
        if UVValidity.COMPATIBILITY in values:
            return UVValidity.COMPATIBILITY
        return UVValidity.EXACT


def face_has_uv(cube_uv, face_name):
    """Return whether a face has an authored UV entry, valid or otherwise."""
    if not isinstance(cube_uv, dict):
        return True
    if face_name not in cube_uv or cube_uv[face_name] is None:
        return False
    return isinstance(cube_uv[face_name], (dict, list, tuple))


def face_has_geometry(face_name, sx, sy, sz):
    if face_name in ("west", "east"):
        return abs(sz) > EPSILON and abs(sy) > EPSILON
    if face_name in ("north", "south"):
        return abs(sx) > EPSILON and abs(sy) > EPSILON
    if face_name in ("up", "down"):
        return abs(sx) > EPSILON and abs(sz) > EPSILON
    return False


def uv_corners_are_valid(uv_corners):
    if not isinstance(uv_corners, (list, tuple)) or len(uv_corners) != 4:
        return False
    for corner in uv_corners:
        if not isinstance(corner, (list, tuple)) or len(corner) != 2:
            return False
        u, v = corner
        if not isinstance(u, (int, float)) or not isinstance(v, (int, float)):
            return False
        if not math.isfinite(float(u)) or not math.isfinite(float(v)):
            return False
    return True


def _version_tuple(value):
    parts = (
        list(value)
        if isinstance(value, (list, tuple))
        else str(value or "").split(".")
    )
    result = []
    for part in parts[:3]:
        try:
            result.append(int(part))
        except (TypeError, ValueError):
            result.append(0)
    while len(result) < 3:
        result.append(0)
    return tuple(result)


def _schema_version_text(value):
    version = _version_tuple(value or "1.21.0")
    return ".".join(str(part) for part in version)


def _finite_number(value, source_location):
    if isinstance(value, bool):
        raise InvalidUVMapping(source_location, "expected a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise InvalidUVMapping(source_location, "expected a finite number") from None
    if not math.isfinite(result):
        raise InvalidUVMapping(source_location, "number must be finite")
    return result


def _numeric_pair(value, source_location):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise InvalidUVMapping(source_location, "expected a two-number array")
    return (
        _finite_number(value[0], f"{source_location}[0]"),
        _finite_number(value[1], f"{source_location}[1]"),
    )


def _rotation_metadata(face_data, schema_version, source_location, issues):
    key = (
        "uv_rotation"
        if "uv_rotation" in face_data
        else ("rotation" if "rotation" in face_data else "")
    )
    if not key:
        return 0.0, UVValidity.EXACT

    rotation_location = f"{source_location}.{key}"
    rotation = _finite_number(face_data[key], rotation_location)
    cardinal = any(
        abs(rotation - documented) <= EPSILON
        for documented in (0.0, 90.0, 180.0, 270.0)
    )
    exact = (
        key == "uv_rotation"
        and _version_tuple(schema_version) >= (1, 21, 0)
        and cardinal
    )
    if exact:
        return rotation, UVValidity.EXACT

    validity = (
        UVValidity.COMPATIBILITY if cardinal else UVValidity.VISUAL_ONLY
    )
    if key == "rotation":
        reason = "nonstandard per-face rotation alias"
    elif _version_tuple(schema_version) < (1, 21, 0):
        reason = (
            f"uv_rotation is not part of geometry {schema_version}"
        )
    else:
        reason = (
            "documented uv_rotation values are 0, 90, 180, and 270"
        )
    fallback = (
        "preserved using geometry 1.21 quarter-turn semantics"
        if cardinal
        else "preserved as a Blender-only arbitrary UV rotation"
    )
    issues.append(UVBuildIssue(
        code="MCB-W200",
        severity="WARNING",
        source_location=rotation_location,
        message=reason,
        fallback=fallback,
        validity=validity,
    ))
    return rotation, validity


def _rotate_uv_corners(uvs, rotation):
    quarter_turns = round(rotation / 90.0)
    if abs(rotation - quarter_turns * 90.0) <= EPSILON:
        turns = int(quarter_turns) % 4
        result = list(uvs)
        for _ in range(turns):
            result = result[-1:] + result[:-1]
        return tuple(result)

    # Unsupported finite angles remain visually inspectable in Blender. In
    # normalized UV coordinates, positive rotation matches Bedrock's clockwise
    # image-space convention because the image V axis is inverted.
    center_u = sum(corner[0] for corner in uvs) / 4.0
    center_v = sum(corner[1] for corner in uvs) / 4.0
    radians = math.radians(rotation)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    result = []
    for u, v in uvs:
        offset_u = u - center_u
        offset_v = v - center_v
        result.append((
            center_u + offset_u * cosine - offset_v * sine,
            center_v + offset_u * sine + offset_v * cosine,
        ))
    return tuple(result)



def rect_uv(u, v, w, h, tex_w, tex_h, face="default", mirror=False, is_per_face=False):
    u0 = u / tex_w
    u1 = (u + w) / tex_w
    top = 1.0 - (v / tex_h)
    bottom = 1.0 - ((v + h) / tex_h)
    tl = (u0, top)
    tr = (u1, top)
    br = (u1, bottom)
    bl = (u0, bottom)

    # Match UV corners to FACE_INDICES. ``tl`` is the authored ``uv`` start
    # corner and ``br`` is ``uv + uv_size``, so negative sizes flip naturally.
    # These are Blockbench's Bedrock rules (the engine's reference editor):
    # side faces start at their upper corner on the cube's -X edge (north,
    # east) or +X edge (south, west); up starts at its +Z edge on -X, and
    # down starts at its -Z edge on -X. That keeps every box-UV net
    # continuous: the top shares its north edge with the north face. Box UV
    # is the same rule fed the per-face rectangles from ``_box_uv_layouts``.
    # Mirroring flips U as vertex order, not by mutating atlas coordinates.
    orders = {
        ("north", False): (bl, tl, tr, br),
        ("south", False): (br, bl, tl, tr),
        ("east", False): (br, bl, tl, tr),
        ("west", False): (bl, tl, tr, br),
        ("up", False): (bl, tl, tr, br),
        ("down", False): (tl, tr, br, bl),
        ("north", True): (br, tr, tl, bl),
        ("south", True): (bl, br, tr, tl),
        ("east", True): (bl, br, tr, tl),
        ("west", True): (br, tr, tl, bl),
        ("up", True): (br, tr, tl, bl),
        ("down", True): (tr, tl, bl, br),
    }
    return tuple(orders.get((face, bool(mirror)), (bl, br, tr, tl)))

def _default_uv_size(face_name, sx, sy, sz):
    if face_name in ("up", "down"):
        return sx, sz
    if face_name in ("east", "west"):
        return sz, sy
    return sx, sy


def _per_face_uv_mapping(
    face_data,
    order_face,
    uv_sx,
    uv_sy,
    uv_sz,
    tex_w,
    tex_h,
    cube_mirror,
    schema_version,
    source_location,
    issues,
):
    """Normalize one declared per-face rectangle into a canonical mapping."""
    if isinstance(face_data, (list, tuple)):
        face_data = {"uv": face_data}
    elif not isinstance(face_data, dict):
        raise InvalidUVMapping(
            source_location, "per-face UV entry must be an object or array"
        )

    default_w, default_h = _default_uv_size(
        order_face, uv_sx, uv_sy, uv_sz
    )
    u, v = _numeric_pair(
        face_data.get("uv", (0.0, 0.0)),
        f"{source_location}.uv",
    )
    w, h = _numeric_pair(
        face_data.get("uv_size", (default_w, default_h)),
        f"{source_location}.uv_size",
    )
    if abs(w) <= EPSILON or abs(h) <= EPSILON:
        issues.append(UVBuildIssue(
            code="MCB-W200",
            severity="WARNING",
            source_location=f"{source_location}.uv_size",
            message="UV rectangle has no renderable area",
            fallback="skipped only this face",
            validity=UVValidity.COMPATIBILITY,
        ))
        return None

    rotation, validity = _rotation_metadata(
        face_data, schema_version, source_location, issues
    )
    face_mirror = bool(cube_mirror or face_data.get("mirror", False))
    corners = rect_uv(
        u,
        v,
        w,
        h,
        tex_w,
        tex_h,
        face=order_face,
        mirror=face_mirror,
        is_per_face=True,
    )
    corners = _rotate_uv_corners(corners, rotation)
    if not uv_corners_are_valid(corners):
        raise InvalidUVMapping(
            source_location, "UV rectangle cannot be rendered"
        )
    return UVMapping(
        origin=(u, v),
        size=(w, h),
        rotation=rotation,
        ordered_corners=corners,
        schema_version=schema_version,
        validity=validity,
    )


def _array_uv_mapping(
    face_name,
    uv,
    sx,
    sy,
    sz,
    tex_w,
    tex_h,
    mirror,
    schema_version,
    source_location,
    float_size=False,
):
    u, v = _numeric_pair(uv, source_location)
    # Box UV is exactly its per-face rectangles (down has a negative height),
    # mapped by the same corner rule as explicit per-face UVs.
    fu, fv, fw, fh = _box_uv_layouts(
        (u, v), sx, sy, sz, mirror, float_size=float_size
    )[face_name]
    corners = rect_uv(
        fu,
        fv,
        fw,
        fh,
        tex_w,
        tex_h,
        face=face_name,
        mirror=mirror,
        is_per_face=False,
    )

    if not uv_corners_are_valid(corners):
        raise InvalidUVMapping(
            source_location, "box UV rectangle cannot be rendered"
        )
    return UVMapping(
        origin=(fu, fv),
        size=(fw, fh),
        rotation=0.0,
        ordered_corners=corners,
        schema_version=schema_version,
        validity=UVValidity.EXACT,
    )


def _box_uv_size(value):
    """Box UV lays a cube out at whole-pixel sizes (Blockbench and the game).

    Blockbench floors each size for Bedrock box UV (``Cube.size(axis,
    'box_uv')``), and vanilla textures are painted for it: the spyglass's
    6.2 x 2.2 x 2.2 eyepiece fills its 16-pixel texture exactly at 6 x 2 x 2.
    """
    value = float(value)
    return math.copysign(math.floor(abs(value) + 1.0e-7), value)


def _box_uv_layouts(uv, sx, sy, sz, mirror, float_size=False):
    """Return canonical pixel rectangles, laid out at whole-pixel sizes.

    ``float_size`` keeps fractional sizes, as Blockbench's Java Edition
    ``modded_entity`` format does (``box_uv_float_size``).
    """
    u, v = float(uv[0]), float(uv[1])
    if float_size:
        sx, sy, sz = float(sx), float(sy), float(sz)
    else:
        sx, sy, sz = (_box_uv_size(value) for value in (sx, sy, sz))
    first_side = (u, v + sz, sz, sy)
    third_side = (u + sz + sx, v + sz, sz, sy)
    return {
        "west": first_side if mirror else third_side,
        "north": (u + sz, v + sz, sx, sy),
        "east": third_side if mirror else first_side,
        "south": (u + sz + sx + sz, v + sz, sx, sy),
        "up": (u + sz, v, sx, sz),
        "down": (u + sz + sx, v + sz, sx, -sz),
    }


def _preferred_box_plane_face(sx, sy, sz):
    """Return Bedrock's rendered cap for an implicit flat box-UV cube."""
    if abs(sx) <= EPSILON:
        return "east"
    if abs(sy) <= EPSILON:
        return "up"
    return "north"


def _java_box_plane_face(sx, sy, sz, mirror):
    """The cap Java Edition shows on top for a flat box-UV cube.

    ``ModelPart.Cube`` draws both caps of a flat box (entity render types do
    not cull) in the order east, west, down, up, north, south, and the later
    quad wins where it is opaque. Java model space points Y down, so its
    ``UP`` quad is the box's underside, the cap named ``down`` here. The west
    quad always samples the first side strip, which this module names
    ``east`` unless the box is mirrored.
    """
    if abs(sx) <= EPSILON:
        return "west" if mirror else "east"
    if abs(sy) <= EPSILON:
        return "down"
    return "south"

def _explicit_plane_sized_entry(face_data, sx, sy, sz):
    """Whether a nominally collapsed face authors a full plane rectangle."""
    if not isinstance(face_data, dict):
        return False
    raw_size = face_data.get("uv_size")
    if not isinstance(raw_size, (list, tuple)) or len(raw_size) < 2:
        return False
    try:
        uv_span = sorted((abs(float(raw_size[0])), abs(float(raw_size[1]))))
    except (TypeError, ValueError):
        return False
    plane_span = sorted(
        abs(float(value)) for value in (sx, sy, sz) if abs(float(value)) > EPSILON
    )
    return (
        len(plane_span) == 2
        and uv_span[0] > EPSILON
        and all(abs(left - right) <= EPSILON for left, right in zip(uv_span, plane_span))
    )


def per_face_uv_is_built(cube_uv, face_name, sx, sy, sz):
    """Whether a per-face UV entry becomes a face (``sx..sz`` after inflate).

    Faces without area are never built, except the authored plane-sized face
    of a flat cube, which renders as that plane.
    """
    if face_name not in cube_uv or cube_uv[face_name] is None:
        return False
    if face_has_geometry(face_name, sx, sy, sz):
        return True
    zero_count = sum(abs(value) <= EPSILON for value in (sx, sy, sz))
    return zero_count == 1 and _explicit_plane_sized_entry(
        cube_uv[face_name], sx, sy, sz
    )


def _iter_candidate_faces(cube_uv, sx, sy, sz, legacy_uv_mode, java_plane_face=None):
    zero_x = abs(sx) <= EPSILON
    zero_y = abs(sy) <= EPSILON
    zero_z = abs(sz) <= EPSILON
    zero_count = int(zero_x) + int(zero_y) + int(zero_z)

    if isinstance(cube_uv, dict):
        for face_name in FACE_ORDER:
            # Presence is authoritative. Validation happens while building the
            # mapping so malformed or zero-area rectangles cannot disappear as
            # if the source face had never been authored.
            if per_face_uv_is_built(cube_uv, face_name, sx, sy, sz):
                yield face_name
        return

    if zero_count == 1:
        # Implicit box UV planes use one canonical cap rendered double-sided.
        # The opposite box-atlas rectangle is frequently transparent padding,
        # so treating it as a second authored face makes wings/tails disappear
        # from one viewing direction.
        yield java_plane_face or _preferred_box_plane_face(sx, sy, sz)
        return

    for face_name in FACE_ORDER:
        if face_has_geometry(face_name, sx, sy, sz):
            yield face_name


def _flat_cap_pair(sx, sy, sz):
    """Return the authored face pair for one collapsed cube axis."""
    zero_x = abs(sx) <= EPSILON
    zero_y = abs(sy) <= EPSILON
    zero_z = abs(sz) <= EPSILON
    if int(zero_x) + int(zero_y) + int(zero_z) != 1:
        return None
    if zero_x:
        return ("west", "east")
    if zero_y:
        return ("down", "up")
    return ("north", "south")


def _uvs_remapped_by_collapsed_position(
    source_indices,
    source_uvs,
    target_indices,
    sx,
    sy,
    sz,
):
    """Attach an opposing cap's UVs to the target cap's vertex order.

    Opposing cube caps use different vertex indices even when a dimension is
    zero. Their positions coincide, so position matching is the stable way to
    preserve the source face's exact authored orientation on one Blender quad.
    """
    vertices = _cube_vertices(0.0, 0.0, 0.0, sx, sy, sz)
    source_by_index = dict(zip(source_indices, source_uvs))
    remapped = []
    for target_index in target_indices:
        target = vertices[target_index]
        match = next(
            (
                source_index
                for source_index in source_indices
                if all(
                    abs(vertices[source_index][axis] - target[axis]) <= EPSILON
                    for axis in range(3)
                )
            ),
            None,
        )
        if match is None:
            return None
        remapped.append(source_by_index[match])
    return tuple(remapped)


def _face_material_instance(face_data):
    if not isinstance(face_data, dict):
        return ""
    value = face_data.get("material_instance", "")
    return str(value) if value is not None else ""


def _pair_flat_cap_faces(result, sx, sy, sz):
    """Collapse explicit plane-sized candidates into one directional quad."""
    if _flat_cap_pair(sx, sy, sz) is None or len(result.faces) < 2:
        return
    preferred = _preferred_box_plane_face(sx, sy, sz)
    front = next(
        (face for face in result.faces if face.direction == preferred),
        result.faces[0],
    )
    alternatives = [face for face in result.faces if face is not front]
    back = alternatives[0]
    if len(alternatives) > 1:
        result.issues.append(UVBuildIssue(
            code="MCB-W201",
            severity="WARNING",
            source_location=front.source_location.rsplit(".uv.", 1)[0] + ".uv",
            message=(
                "more than two plane-sized UV candidates target one "
                "zero-thickness polygon"
            ),
            fallback=(
                "retained the preferred cap and first authored alternate "
                "in canonical face order"
            ),
            validity=UVValidity.COMPATIBILITY,
        ))
    back_uvs = _uvs_remapped_by_collapsed_position(
        back.indices,
        back.uv_corners,
        front.indices,
        sx,
        sy,
        sz,
    )
    if back_uvs is None or not uv_corners_are_valid(back_uvs):
        raise InvalidUVMapping(
            back.source_location,
            "opposing plane UVs cannot be matched to the physical polygon",
        )
    front.back_uv = replace(back.front_uv, ordered_corners=back_uvs)
    front.back_material = back.front_material
    front.back_source_location = back.source_location
    front.back_direction = back.direction
    front.two_sided = True
    front.pair_mode = "directional"
    result.faces = [front]
    result.faces_generated = 1
    result.paired_two_sided_planes += 1

_OPPOSITE_FACE = {
    "west": "east", "east": "west",
    "down": "up", "up": "down",
    "north": "south", "south": "north",
}


def _attach_implicit_box_pair(
    result, cube_uv, sx, sy, sz, uv_sx, uv_sy, uv_sz,
    tex_w, tex_h, mirror, schema_version, source_location, float_size=False,
):
    """Retain both implicit box-atlas cap candidates on one physical quad."""
    if len(result.faces) != 1:
        return
    front = result.faces[0]
    back_name = _OPPOSITE_FACE.get(front.direction, "")
    if not back_name:
        return
    back_location = f"{source_location}.uv#{back_name}"
    back_mapping = _array_uv_mapping(
        back_name,
        cube_uv,
        uv_sx,
        uv_sy,
        uv_sz,
        tex_w,
        tex_h,
        mirror,
        schema_version,
        back_location,
        float_size=float_size,
    )
    back_uvs = _uvs_remapped_by_collapsed_position(
        FACE_INDICES[back_name],
        back_mapping.ordered_corners,
        front.indices,
        sx,
        sy,
        sz,
    )
    if back_uvs is None or not uv_corners_are_valid(back_uvs):
        raise InvalidUVMapping(
            back_location,
            "implicit opposing plane UVs cannot be matched to the physical polygon",
        )
    front.back_uv = replace(back_mapping, ordered_corners=back_uvs)
    front.back_source_location = back_location
    front.back_direction = back_name
    front.two_sided = True
    front.pair_mode = "composite"
    result.paired_two_sided_planes += 1


def _cube_vertices(ox, oy, oz, sx, sy, sz):
    return [
        (ox, oy, oz),
        (ox + sx, oy, oz),
        (ox + sx, oy + sy, oz),
        (ox, oy + sy, oz),
        (ox, oy, oz + sz),
        (ox + sx, oy, oz + sz),
        (ox + sx, oy + sy, oz + sz),
        (ox, oy + sy, oz + sz),
    ]


def build_cube_faces(
    cube,
    ox,
    oy,
    oz,
    sx,
    sy,
    sz,
    tex_w,
    tex_h,
    mirror=False,
    legacy_uv_mode=False,
    debug_uv=False,
    uv_sx=None,
    uv_sy=None,
    uv_sz=None,
    format_version="1.21.0",
    source_location="$",
):
    """Build canonical face records from one Bedrock cube definition."""
    source_location = str(source_location or "$")
    if not isinstance(cube, dict):
        raise InvalidUVMapping(source_location, "cube must be an object")

    ox = _finite_number(ox, f"{source_location}.origin[0]")
    oy = _finite_number(oy, f"{source_location}.origin[1]")
    oz = _finite_number(oz, f"{source_location}.origin[2]")
    sx = _finite_number(sx, f"{source_location}.size[0]")
    sy = _finite_number(sy, f"{source_location}.size[1]")
    sz = _finite_number(sz, f"{source_location}.size[2]")
    uv_sx = sx if uv_sx is None else _finite_number(
        uv_sx, f"{source_location}.size[0]"
    )
    uv_sy = sy if uv_sy is None else _finite_number(
        uv_sy, f"{source_location}.size[1]"
    )
    uv_sz = sz if uv_sz is None else _finite_number(
        uv_sz, f"{source_location}.size[2]"
    )
    tex_w = _finite_number(tex_w, "$.description.texture_width")
    tex_h = _finite_number(tex_h, "$.description.texture_height")
    if tex_w <= EPSILON or tex_h <= EPSILON:
        raise InvalidUVMapping(
            "$.description",
            "texture_width and texture_height must both be positive",
        )
    schema_version = _schema_version_text(format_version)
    result = CubeBuildResult()

    # Topology follows inflated/deflated dimensions. UV rectangles continue to
    # use the uninflated authored dimensions supplied through uv_s*.
    zero_x = abs(sx) <= EPSILON
    zero_y = abs(sy) <= EPSILON
    zero_z = abs(sz) <= EPSILON
    zero_count = int(zero_x) + int(zero_y) + int(zero_z)
    if zero_count >= 2:
        return result
    if zero_count == 1:
        result.zero_thickness_planes += 1

    result.vertices = _cube_vertices(ox, oy, oz, sx, sy, sz)
    cube_uv = cube.get("uv", (0.0, 0.0))
    if not isinstance(cube_uv, (dict, list, tuple)):
        raise InvalidUVMapping(
            f"{source_location}.uv",
            "cube UV must be a two-number array or per-face object",
        )
    cube_mirror = bool(mirror or cube.get("mirror", False))
    float_box_uv = bool(cube.get("_mcbedrock_box_uv_float_size", False))
    # Java Edition entity cubes (mod models) draw flat boxes in the game's
    # cap order. Every other model keeps Bedrock's cap.
    java_plane_face = (
        _java_box_plane_face(sx, sy, sz, cube_mirror)
        if cube.get("_mcbedrock_java_entity_cube") and zero_count == 1
        and not isinstance(cube_uv, dict)
        else None
    )

    for face_name in _iter_candidate_faces(
        cube_uv, sx, sy, sz, legacy_uv_mode, java_plane_face
    ):
        proxy_plane_face = (
            zero_count == 1
            and isinstance(cube_uv, dict)
            and _explicit_plane_sized_entry(cube_uv.get(face_name), sx, sy, sz)
        )
        if not face_has_geometry(face_name, sx, sy, sz) and not proxy_plane_face:
            result.faces_skipped_degenerate += 1
            continue
        physical_face_name = (
            _preferred_box_plane_face(sx, sy, sz)
            if proxy_plane_face and not face_has_geometry(face_name, sx, sy, sz)
            else face_name
        )

        if isinstance(cube_uv, dict):
            face_data = cube_uv[face_name]
            face_location = f"{source_location}.uv.{face_name}"
            mapping = _per_face_uv_mapping(
                face_data,
                face_name,
                uv_sx,
                uv_sy,
                uv_sz,
                tex_w,
                tex_h,
                cube_mirror,
                schema_version,
                face_location,
                result.issues,
            )
            if mapping is None:
                result.faces_skipped_no_uv += 1
                continue
        else:
            face_data = None
            face_location = f"{source_location}.uv#{face_name}"
            mapping = _array_uv_mapping(
                face_name,
                cube_uv,
                uv_sx,
                uv_sy,
                uv_sz,
                tex_w,
                tex_h,
                cube_mirror,
                schema_version,
                face_location,
                float_size=float_box_uv,
            )

        result.faces.append(FaceRecord(
            source_location=face_location,
            direction=face_name,
            vertex_indices=FACE_INDICES[physical_face_name],
            front_uv=mapping,
            front_material=_face_material_instance(face_data),
        ))
        result.faces_generated += 1
        if debug_uv:
            print(
                f"[mcbedrock uv] source={face_location} face={face_name} "
                f"uv={mapping.ordered_corners} validity={mapping.validity.value} "
                f"size=({sx},{sy},{sz}) "
                f"uv_size=({uv_sx},{uv_sy},{uv_sz}) "
                f"tex={tex_w}x{tex_h}"
            )

    if zero_count == 1:
        if isinstance(cube_uv, dict):
            _pair_flat_cap_faces(result, sx, sy, sz)
        else:
            _attach_implicit_box_pair(
                result,
                cube_uv,
                sx,
                sy,
                sz,
                uv_sx,
                uv_sy,
                uv_sz,
                tex_w,
                tex_h,
                cube_mirror,
                schema_version,
                source_location,
                float_size=float_box_uv,
            )
        # A collapsed cube is a renderer card, regardless of whether its UVs
        # author one cap or both opposing caps. Bedrock displays cards from
        # both directions; leaving a single-cap card one-sided makes foliage,
        # horns, tendrils, feathers, and other pixel-thin layers disappear
        # whenever the viewer reaches their back face. Distinct opposing UVs
        # are still preserved above when the source supplies them.
        for face in result.faces:
            face.two_sided = True
            face.two_sided_reason = "collapsed_plane"

    return result


def mark_see_through_faces(result, face_has_visible_texels):
    """Keep faces double-sided when the game shows them through their cube.

    Bedrock's alpha-test entity materials draw without culling, so a painted
    face behind a fully transparent (or missing) opposite face is seen from
    the front too: chicken and parrot legs are one painted side of a
    see-through box. Those faces stay two-sided; every other closed face can
    be culled.
    ``face_has_visible_texels(face)`` reports whether a face's UV area has
    any texel that renders.
    """
    visible = {
        face.direction: bool(face_has_visible_texels(face))
        for face in result.faces
    }
    marked = 0
    for face in result.faces:
        if face.two_sided or not visible.get(face.direction):
            continue
        if visible.get(_OPPOSITE_FACE.get(face.direction, ""), False):
            continue
        face.two_sided = True
        face.two_sided_reason = "see_through_cube"
        marked += 1
    return marked


def describe_face_uv_rect(face_name, cube_uv, sx, sy, sz, tex_w, tex_h, mirror=False):
    """Debug helper: return pixel-space UV rect for a face, or None if skipped."""
    cube_mirror = bool(mirror)
    if isinstance(cube_uv, dict):
        if not face_has_uv(cube_uv, face_name):
            return None
        face_data = cube_uv.get(face_name)
        if isinstance(face_data, list):
            face_data = {"uv": face_data}
        face_uv = face_data.get("uv", [0, 0])
        default_w, default_h = _default_uv_size(face_name, sx, sy, sz)
        face_size = face_data.get("uv_size", [default_w, default_h])
        u = coerce_float(face_uv[0] if len(face_uv) > 0 else 0)
        v = coerce_float(face_uv[1] if len(face_uv) > 1 else 0)
        w = coerce_float(face_size[0] if len(face_size) > 0 else default_w)
        h = coerce_float(face_size[1] if len(face_size) > 1 else default_h)
        return {"face": face_name, "uv": [u, v], "uv_size": [w, h], "rotation": face_data.get("uv_rotation", face_data.get("rotation", 0))}

    layouts = _box_uv_layouts(cube_uv, sx, sy, sz, cube_mirror)
    if face_name not in layouts:
        return None
    fu, fv, fw, fh = layouts[face_name]
    return {"face": face_name, "uv": [fu, fv], "uv_size": [fw, fh], "rotation": 0}
