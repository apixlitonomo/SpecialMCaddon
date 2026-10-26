"""Structured, source-located diagnostics for Bedrock visual imports."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math

try:
    from ..builders.bedrock_uv import per_face_uv_is_built
except (ImportError, ValueError):  # pragma: no cover - plain Python tests
    from builders.bedrock_uv import per_face_uv_is_built


FACE_NAMES = {"north", "south", "east", "west", "up", "down"}
ISSUE_CODES = {
    "MCB-E100": "invalid geometry/schema",
    "MCB-E110": "missing geometry reference",
    "MCB-E120": "missing or unresolved texture",
    "MCB-E130": "invalid or out-of-range UV",
    "MCB-E140": "missing or cyclic bone hierarchy",
    "MCB-E150": "unresolved renderer or block payload",
    "MCB-W200": "unsupported optional feature",
    "MCB-W210": "recoverable material fallback",
    "MCB-W220": "UV reaches past the texture edge",
    "MCB-W230": "missing parent bone (imported as a root bone)",
    "MCB-W240": "texture mesh skipped (no texture)",
}
# Data the game still renders. These import with a warning; every other
# problem stays fatal.
RELAXED_ISSUE_CODES = frozenset({"MCB-W220", "MCB-W230", "MCB-W240"})
SUPPORTED_GEOMETRY_KEYS = {
    "description",
    "bones",
    "cape",
    "item_display_transforms",
}
SUPPORTED_DESCRIPTION_KEYS = {
    "identifier",
    "texture_width",
    "texture_height",
    "visible_bounds_width",
    "visible_bounds_height",
    "visible_bounds_offset",
    "legacy_uv",
    "inherits",
}
SUPPORTED_BONE_KEYS = {
    "name",
    "parent",
    "pivot",
    "rotation",
    "binding",
    "mirror",
    "inflate",
    "bind_pose_rotation",
    "neverRender",
    "never_render",
    "locators",
    "poly_mesh",
    "texture_meshes",
    "cubes",
    "debug",
    "reset",
    "render_group_id",
}
SUPPORTED_CUBE_KEYS = {
    "origin",
    "size",
    "uv",
    "pivot",
    "rotation",
    "inflate",
    "mirror",
}
SUPPORTED_POLY_MESH_KEYS = {
    "normalized_uvs",
    "positions",
    "normals",
    "uvs",
    "polys",
}
SUPPORTED_TEXTURE_MESH_KEYS = {
    "texture",
    "position",
    "local_pivot",
    "rotation",
    "scale",
    "use_pixel_depth",
}
SUPPORTED_UV_FACE_KEYS = {
    "uv",
    "uv_size",
    "uv_rotation",
    "rotation",
    "material_instance",
}
SUPPORTED_LOCATOR_KEYS = {
    "offset",
    "rotation",
    "ignore_inherited_scale",
}
CARDINAL_UV_ROTATIONS = {0.0, 90.0, 180.0, 270.0}


@dataclass(frozen=True)
class ImportIssue:
    code: str
    severity: str
    entity: str = ""
    geometry: str = ""
    source_path: str = ""
    source_location: str = "$"
    bone_cube_path: str = ""
    message: str = ""
    fallback: str = ""

    @property
    def path(self):
        """Backward-compatible JSON-path spelling used by existing UI code."""
        return self.source_location

    @property
    def detail(self):
        return self.message

    @property
    def fatal(self):
        return self.severity.upper() == "ERROR"

    def as_dict(self):
        return {
            "code": self.code,
            "severity": self.severity,
            "entity": self.entity,
            "geometry": self.geometry,
            "source_path": self.source_path,
            "source_location": self.source_location,
            "bone_cube_path": self.bone_cube_path,
            "message": self.message,
            "fallback": self.fallback,
            "path": self.source_location,
            "detail": self.message,
        }


# Kept as a public alias so existing imports continue to work.
VisualCompatibilityIssue = ImportIssue


@dataclass(frozen=True)
class ImportReport:
    issues: tuple = ()
    created_datablocks: tuple = ()
    source_provenance: tuple = ()

    @property
    def fatal(self):
        return tuple(issue for issue in self.issues if issue.fatal)

    @property
    def warnings(self):
        return tuple(issue for issue in self.issues if not issue.fatal)

    @property
    def cancelled(self):
        return bool(self.fatal)

    def as_dict(self):
        return {
            "cancelled": self.cancelled,
            "issues": [issue.as_dict() for issue in self.issues],
            "created_datablocks": list(self.created_datablocks),
            "source_provenance": list(self.source_provenance),
        }

    def to_json(self):
        return json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )


class VisualCompatibilityError(ValueError):
    """Fatal, source-located validation error raised before Blender mutation."""

    def __init__(
        self,
        identifier,
        format_version,
        issues,
        report=None,
    ):
        self.identifier = str(identifier or "unknown")
        self.format_version = str(format_version or "unknown")
        self.issues = tuple(issues)
        self.report = report or ImportReport(self.issues)
        summary = "; ".join(
            f"{issue.source_location}: {issue.message}"
            for issue in self.issues[:5]
        )
        if len(self.issues) > 5:
            summary += f"; and {len(self.issues) - 5} more"
        super().__init__(
            f"Cannot faithfully import {self.identifier} "
            f"(geometry {self.format_version}): {summary}"
        )


def _version_tuple(value):
    if isinstance(value, (tuple, list)):
        values = value
    else:
        values = str(value or "").split(".")
    parsed = []
    for value in values[:3]:
        try:
            parsed.append(int(value))
        except (TypeError, ValueError):
            parsed.append(0)
    return tuple((parsed + [0, 0, 0])[:3])


def _geometry_identifier(geometry):
    description = (
        geometry.get("description", {})
        if isinstance(geometry, dict)
        else {}
    )
    if not isinstance(description, dict):
        return ""
    return str(description.get("identifier", ""))


def _new_issue(
    code,
    severity,
    geometry,
    source_path,
    source_location,
    message,
    fallback="",
    entity="",
    bone_cube_path="",
):
    return ImportIssue(
        code=code,
        severity=severity,
        entity=str(entity or ""),
        geometry=_geometry_identifier(geometry),
        source_path=str(source_path or ""),
        source_location=source_location,
        bone_cube_path=bone_cube_path,
        message=message,
        fallback=fallback,
    )


def missing_geometry_issue(
    reference,
    *,
    entity="",
    source_path="",
    source_location="$.description.geometry",
):
    """Fatal diagnostic for a declared geometry that cannot be resolved."""
    return ImportIssue(
        code="MCB-E110",
        severity="ERROR",
        entity=str(entity or ""),
        source_path=str(source_path or ""),
        source_location=source_location,
        message=f"Geometry reference {reference!r} could not be resolved",
    )


def missing_texture_issue(
    reference,
    *,
    entity="",
    geometry="",
    source_path="",
    source_location="$.description.textures",
):
    """Fatal diagnostic for a declared base texture that is unavailable."""
    return ImportIssue(
        code="MCB-E120",
        severity="ERROR",
        entity=str(entity or ""),
        geometry=str(geometry or ""),
        source_path=str(source_path or ""),
        source_location=source_location,
        message=f"Texture reference {reference!r} could not be resolved",
    )


def renderer_payload_issue(
    payload,
    *,
    entity="",
    geometry="",
    source_path="",
    source_location="$.render_controller",
):
    """Fatal diagnostic for renderer-owned visual data that is unavailable."""
    return ImportIssue(
        code="MCB-E150",
        severity="ERROR",
        entity=str(entity or ""),
        geometry=str(geometry or ""),
        source_path=str(source_path or ""),
        source_location=source_location,
        message=f"Renderer payload {payload!r} could not be resolved",
    )


def texture_mesh_skipped_issue(
    bone_name,
    source_path="",
    source_location="$",
    geometry="",
):
    return ImportIssue(
        code="MCB-W240",
        severity="WARNING",
        geometry=str(geometry or ""),
        source_path=str(source_path or ""),
        source_location=str(source_location or "$"),
        bone_cube_path=str(source_location or "$"),
        message=f"Texture mesh on bone {str(bone_name)!r} has no texture to draw",
        fallback="skipped; the rest of the model imports",
    )


def relaxed_import_warnings(issues):
    """Short, readable lines for the relaxed-check warnings in ``issues``.

    Accepts ``ImportIssue`` objects or their ``as_dict`` payloads.
    """
    past_edge = 0
    lines = []
    for issue in issues:
        data = issue.as_dict() if hasattr(issue, "as_dict") else dict(issue)
        code = data.get("code")
        if code == "MCB-W220":
            past_edge += 1
        elif code in RELAXED_ISSUE_CODES:
            lines.append(f"{data.get('message', '')} ({data.get('fallback', '')})")
    if past_edge:
        lines.insert(0, (
            f"{past_edge} face(s) reach past the texture edge; "
            "the edge pixels repeat there, as in the game"
        ))
    return lines


def material_fallback_issue(
    message,
    *,
    fallback,
    entity="",
    geometry="",
    source_path="",
    source_location="$.material",
):
    """Recoverable source-located diagnostic for a material fallback."""
    return ImportIssue(
        code="MCB-W210",
        severity="WARNING",
        entity=str(entity or ""),
        geometry=str(geometry or ""),
        source_path=str(source_path or ""),
        source_location=source_location,
        message=str(message),
        fallback=str(fallback),
    )


def _validate_vector(
    value,
    length,
    path,
    issues,
    geometry,
    source_path,
    entity,
    *,
    code="MCB-E100",
    nonnegative=False,
    bone_cube_path="",
):
    valid = isinstance(value, (list, tuple)) and len(value) >= length
    if valid:
        for component in value[:length]:
            if (
                isinstance(component, bool)
                or not isinstance(component, (int, float))
                or not math.isfinite(float(component))
                or (nonnegative and float(component) < 0.0)
            ):
                valid = False
                break
    if valid:
        return True
    requirement = f"{length} finite numbers"
    if nonnegative:
        requirement = f"{length} finite non-negative numbers"
    issues.append(
        _new_issue(
            code,
            "ERROR",
            geometry,
            source_path,
            path,
            f"Value must contain {requirement}",
            entity=entity,
            bone_cube_path=bone_cube_path,
        )
    )
    return False


def _unknown_keys(
    value,
    allowed,
    path,
    issues,
    geometry,
    source_path,
    entity,
):
    if not isinstance(value, dict):
        return
    for key in value:
        if str(key).startswith("_mcbedrock_"):
            continue
        if key not in allowed:
            issues.append(
                _new_issue(
                    "MCB-W200",
                    "WARNING",
                    geometry,
                    source_path,
                    f"{path}.{key}",
                    "Visual field is not implemented",
                    fallback="field ignored; remaining source data preserved",
                    entity=entity,
                )
            )


def _validate_pair(
    value,
    path,
    issues,
    geometry,
    source_path,
    entity,
):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        issues.append(
            _new_issue(
                "MCB-E130",
                "ERROR",
                geometry,
                source_path,
                path,
                "UV value must contain two finite numbers",
                entity=entity,
            )
        )
        return
    for component in value[:2]:
        if (
            isinstance(component, bool)
            or not isinstance(component, (int, float))
            or not math.isfinite(float(component))
        ):
            issues.append(
                _new_issue(
                    "MCB-E130",
                    "ERROR",
                    geometry,
                    source_path,
                    path,
                    "UV value must contain two finite numbers",
                    entity=entity,
                )
            )
            return


def _validate_uv_rotation(
    face,
    face_path,
    schema_family,
    issues,
    geometry,
    source_path,
    entity,
):
    has_standard = "uv_rotation" in face
    has_legacy_alias = "rotation" in face
    if not has_standard and not has_legacy_alias:
        return
    raw = face.get(
        "uv_rotation",
        face.get("rotation"),
    )
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(float(raw))
    ):
        issues.append(
            _new_issue(
                "MCB-E130",
                "ERROR",
                geometry,
                source_path,
                (
                    f"{face_path}.uv_rotation"
                    if has_standard
                    else f"{face_path}.rotation"
                ),
                "UV rotation must be a finite number",
                entity=entity,
            )
        )
        return

    rotation = float(raw) % 360.0
    if (
        has_standard
        and schema_family >= (1, 21, 0)
        and rotation in CARDINAL_UV_ROTATIONS
    ):
        return

    if rotation in CARDINAL_UV_ROTATIONS:
        issues.append(
            _new_issue(
                "MCB-W200",
                "WARNING",
                geometry,
                source_path,
                (
                    f"{face_path}.uv_rotation"
                    if has_standard
                    else f"{face_path}.rotation"
                ),
                "UV rotation is not part of the declared geometry schema",
                fallback=(
                    "preserved with documented geometry 1.21 "
                    "clockwise rotation semantics"
                ),
                entity=entity,
            )
        )
        return

    issues.append(
        _new_issue(
            "MCB-W200",
            "WARNING",
            geometry,
            source_path,
            (
                f"{face_path}.uv_rotation"
                if has_standard
                else f"{face_path}.rotation"
            ),
            "UV rotation cannot be encoded by the declared Bedrock schema",
            fallback="preserved as a Blender visual-only UV transform",
            entity=entity,
        )
    )


def _validate_uv_bounds(
    origin,
    size,
    texture_width,
    texture_height,
    path,
    issues,
    geometry,
    source_path,
    entity,
    bone_cube_path,
):
    if not (
        isinstance(origin, (list, tuple))
        and len(origin) >= 2
        and isinstance(size, (list, tuple))
        and len(size) >= 2
    ):
        return
    values = tuple(origin[:2]) + tuple(size[:2])
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in values
    ):
        return
    if not (
        isinstance(texture_width, (int, float))
        and isinstance(texture_height, (int, float))
        and texture_width > 0
        and texture_height > 0
    ):
        return
    u0, v0 = map(float, origin[:2])
    du, dv = map(float, size[:2])
    epsilon = 1.0e-6
    if (
        min(u0, u0 + du) < -epsilon
        or max(u0, u0 + du) > float(texture_width) + epsilon
        or min(v0, v0 + dv) < -epsilon
        or max(v0, v0 + dv) > float(texture_height) + epsilon
    ):
        # The game clamps entity textures, so the part past the edge shows
        # the edge pixels (the importer samples with EXTEND to match).
        issues.append(
            _new_issue(
                "MCB-W220",
                "WARNING",
                geometry,
                source_path,
                path,
                (
                    "UV rectangle extends outside texture bounds "
                    f"{float(texture_width):g}x{float(texture_height):g}"
                ),
                entity=entity,
                bone_cube_path=bone_cube_path,
                fallback="edge pixels repeat past the texture edge, as in the game",
            )
        )


def _validate_hierarchy(
    bones,
    issues,
    geometry,
    source_path,
    entity,
):
    locations = {}
    parents = {}
    parent_entries = []
    for index, bone in enumerate(bones):
        if not isinstance(bone, dict):
            continue
        name = str(bone.get("name", ""))
        key = name.casefold()
        path = f"$.bones[{index}]"
        if not name:
            issues.append(
                _new_issue(
                    "MCB-E140",
                    "ERROR",
                    geometry,
                    source_path,
                    f"{path}.name",
                    "Bone name must be non-empty",
                    entity=entity,
                    bone_cube_path=path,
                )
            )
            continue
        locations.setdefault(key, path)
        parent = bone.get("parent")
        if parent:
            parent_key = str(parent).casefold()
            if parent_key != key:
                parents.setdefault(key, parent_key)
                parent_entries.append((key, parent_key, path))

    for name, parent, _entry_path in parent_entries:
        if parent not in locations:
            # The builder roots such a bone like any parentless bone (an
            # attachable bound to the player's arm, or a renamed parent).
            path = locations.get(name, "$.bones")
            issues.append(
                _new_issue(
                    "MCB-W230",
                    "WARNING",
                    geometry,
                    source_path,
                    f"{path}.parent",
                    f"Missing parent bone {parent!r}",
                    entity=entity,
                    bone_cube_path=path,
                    fallback="imported as a root bone",
                )
            )

    visiting = set()
    visited = set()

    def visit(name):
        if name in visited:
            return
        if name in visiting:
            path = locations.get(name, "$.bones")
            issues.append(
                _new_issue(
                    "MCB-E140",
                    "ERROR",
                    geometry,
                    source_path,
                    f"{path}.parent",
                    f"Bone hierarchy cycle includes {name!r}",
                    entity=entity,
                    bone_cube_path=path,
                )
            )
            return
        visiting.add(name)
        parent = parents.get(name)
        if parent in locations:
            visit(parent)
        visiting.discard(name)
        visited.add(name)

    for name in locations:
        visit(name)


def inspect_geometry_visuals(
    geometry,
    source_path="",
    entity="",
):
    """Return structured visual issues using stable MCB issue codes."""
    if not isinstance(geometry, dict):
        return [
            _new_issue(
                "MCB-E100",
                "ERROR",
                {},
                source_path,
                "$",
                "Geometry entry is not an object",
                entity=entity,
            )
        ]

    issues = []
    schema_family = _version_tuple(
        geometry.get(
            "_mcbedrock_schema_family",
            geometry.get("_mcbedrock_format_version", ""),
        )
    )
    _unknown_keys(
        geometry,
        SUPPORTED_GEOMETRY_KEYS,
        "$",
        issues,
        geometry,
        source_path,
        entity,
    )
    description = geometry.get("description", {})
    if not isinstance(description, dict):
        issues.append(
            _new_issue(
                "MCB-E100",
                "ERROR",
                geometry,
                source_path,
                "$.description",
                "Geometry description must be an object",
                entity=entity,
            )
        )
    else:
        _unknown_keys(
            description,
            SUPPORTED_DESCRIPTION_KEYS,
            "$.description",
            issues,
            geometry,
            source_path,
            entity,
        )

    description_values = description if isinstance(description, dict) else {}
    texture_width = description_values.get("texture_width", 64)
    texture_height = description_values.get("texture_height", 64)
    for field_name, dimension in (
        ("texture_width", texture_width),
        ("texture_height", texture_height),
    ):
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, (int, float))
            or not math.isfinite(float(dimension))
            or float(dimension) <= 0.0
        ):
            issues.append(
                _new_issue(
                    "MCB-E100",
                    "ERROR",
                    geometry,
                    source_path,
                    f"$.description.{field_name}",
                    "Texture dimension must be a positive finite number",
                    entity=entity,
                )
            )
    bones = geometry.get("bones", [])
    if not isinstance(bones, list):
        issues.append(
            _new_issue(
                "MCB-E100",
                "ERROR",
                geometry,
                source_path,
                "$.bones",
                "Bones must be an array",
                entity=entity,
            )
        )
        return issues

    _validate_hierarchy(
        bones,
        issues,
        geometry,
        source_path,
        entity,
    )

    for bone_index, bone in enumerate(bones):
        bone_path = f"$.bones[{bone_index}]"
        if not isinstance(bone, dict):
            issues.append(
                _new_issue(
                    "MCB-E100",
                    "ERROR",
                    geometry,
                    source_path,
                    bone_path,
                    "Bone must be an object",
                    entity=entity,
                    bone_cube_path=bone_path,
                )
            )
            continue
        _unknown_keys(
            bone,
            SUPPORTED_BONE_KEYS,
            bone_path,
            issues,
            geometry,
            source_path,
            entity,
        )

        for field_name in ("pivot", "rotation", "bind_pose_rotation"):
            if field_name in bone:
                _validate_vector(
                    bone[field_name],
                    3,
                    f"{bone_path}.{field_name}",
                    issues,
                    geometry,
                    source_path,
                    entity,
                    bone_cube_path=bone_path,
                )
        if "inflate" in bone and (
            isinstance(bone["inflate"], bool)
            or not isinstance(bone["inflate"], (int, float))
            or not math.isfinite(float(bone["inflate"]))
        ):
            issues.append(
                _new_issue(
                    "MCB-E100",
                    "ERROR",
                    geometry,
                    source_path,
                    f"{bone_path}.inflate",
                    "Bone inflate must be a finite number",
                    entity=entity,
                    bone_cube_path=bone_path,
                )
            )
        if "mirror" in bone and not isinstance(bone["mirror"], bool):
            issues.append(
                _new_issue(
                    "MCB-E100",
                    "ERROR",
                    geometry,
                    source_path,
                    f"{bone_path}.mirror",
                    "Bone mirror must be a boolean",
                    entity=entity,
                    bone_cube_path=bone_path,
                )
            )

        poly_mesh = bone.get("poly_mesh")
        if poly_mesh is not None:
            if isinstance(poly_mesh, dict):
                _unknown_keys(
                    poly_mesh,
                    SUPPORTED_POLY_MESH_KEYS,
                    f"{bone_path}.poly_mesh",
                    issues,
                    geometry,
                    source_path,
                    entity,
                )
                issues.append(
                    _new_issue(
                        "MCB-W200",
                        "WARNING",
                        geometry,
                        source_path,
                        f"{bone_path}.poly_mesh",
                        "poly_mesh is deprecated by the Bedrock schema",
                        fallback="valid triangles/quads are preserved directly",
                        entity=entity,
                        bone_cube_path=f"{bone_path}.poly_mesh",
                    )
                )
            else:
                issues.append(
                    _new_issue(
                        "MCB-E100",
                        "ERROR",
                        geometry,
                        source_path,
                        f"{bone_path}.poly_mesh",
                        "poly_mesh must be an object",
                        entity=entity,
                        bone_cube_path=f"{bone_path}.poly_mesh",
                    )
                )

        texture_meshes = bone.get("texture_meshes")
        if texture_meshes is not None:
            if not isinstance(texture_meshes, list):
                issues.append(
                    _new_issue(
                        "MCB-E100",
                        "ERROR",
                        geometry,
                        source_path,
                        f"{bone_path}.texture_meshes",
                        "texture_meshes must be an array",
                        entity=entity,
                        bone_cube_path=f"{bone_path}.texture_meshes",
                    )
                )
            else:
                for texture_index, texture_mesh in enumerate(
                    texture_meshes
                ):
                    texture_path = (
                        f"{bone_path}.texture_meshes[{texture_index}]"
                    )
                    if isinstance(texture_mesh, dict):
                        _unknown_keys(
                            texture_mesh,
                            SUPPORTED_TEXTURE_MESH_KEYS,
                            texture_path,
                            issues,
                            geometry,
                            source_path,
                            entity,
                        )
                    else:
                        issues.append(
                            _new_issue(
                                "MCB-E100",
                                "ERROR",
                                geometry,
                                source_path,
                                texture_path,
                                "Texture mesh entry must be an object",
                                entity=entity,
                                bone_cube_path=texture_path,
                            )
                        )

        locators = bone.get("locators", {})
        if isinstance(locators, dict):
            for locator_name, locator in locators.items():
                if isinstance(locator, dict):
                    _unknown_keys(
                        locator,
                        SUPPORTED_LOCATOR_KEYS,
                        f"{bone_path}.locators.{locator_name}",
                        issues,
                        geometry,
                        source_path,
                        entity,
                    )

        cubes = bone.get("cubes", [])
        if not isinstance(cubes, list):
            issues.append(
                _new_issue(
                    "MCB-E100",
                    "ERROR",
                    geometry,
                    source_path,
                    f"{bone_path}.cubes",
                    "Cubes must be an array",
                    entity=entity,
                    bone_cube_path=f"{bone_path}.cubes",
                )
            )
            continue

        for cube_index, cube in enumerate(cubes):
            cube_path = f"{bone_path}.cubes[{cube_index}]"
            if not isinstance(cube, dict):
                issues.append(
                    _new_issue(
                        "MCB-E100",
                        "ERROR",
                        geometry,
                        source_path,
                        cube_path,
                        "Cube must be an object",
                        entity=entity,
                        bone_cube_path=cube_path,
                    )
                )
                continue
            _unknown_keys(
                cube,
                SUPPORTED_CUBE_KEYS,
                cube_path,
                issues,
                geometry,
                source_path,
                entity,
            )
            origin = cube.get("origin", ())
            size = cube.get("size", ())
            _validate_vector(
                origin,
                3,
                f"{cube_path}.origin",
                issues,
                geometry,
                source_path,
                entity,
                bone_cube_path=cube_path,
            )
            size_valid = _validate_vector(
                size,
                3,
                f"{cube_path}.size",
                issues,
                geometry,
                source_path,
                entity,
                nonnegative=False,
                bone_cube_path=cube_path,
            )
            for field_name in ("pivot", "rotation"):
                if field_name in cube:
                    _validate_vector(
                        cube[field_name],
                        3,
                        f"{cube_path}.{field_name}",
                        issues,
                        geometry,
                        source_path,
                        entity,
                        bone_cube_path=cube_path,
                    )
            if "inflate" in cube and (
                isinstance(cube["inflate"], bool)
                or not isinstance(cube["inflate"], (int, float))
                or not math.isfinite(float(cube["inflate"]))
            ):
                issues.append(
                    _new_issue(
                        "MCB-E100",
                        "ERROR",
                        geometry,
                        source_path,
                        f"{cube_path}.inflate",
                        "Cube inflate must be a finite number",
                        entity=entity,
                        bone_cube_path=cube_path,
                    )
                )
            if "mirror" in cube and not isinstance(cube["mirror"], bool):
                issues.append(
                    _new_issue(
                        "MCB-E100",
                        "ERROR",
                        geometry,
                        source_path,
                        f"{cube_path}.mirror",
                        "Cube mirror must be a boolean",
                        entity=entity,
                        bone_cube_path=cube_path,
                    )
                )

            uv = cube.get("uv")
            if isinstance(uv, (list, tuple)):
                _validate_pair(
                    uv,
                    f"{cube_path}.uv",
                    issues,
                    geometry,
                    source_path,
                    entity,
                )
                continue
            if uv is None:
                continue
            if not isinstance(uv, dict):
                issues.append(
                    _new_issue(
                        "MCB-E130",
                        "ERROR",
                        geometry,
                        source_path,
                        f"{cube_path}.uv",
                        "Cube UV must be a two-number box origin or per-face object",
                        entity=entity,
                        bone_cube_path=cube_path,
                    )
                )
                continue
            for face_name, face in uv.items():
                face_path = f"{cube_path}.uv.{face_name}"
                if face_name not in FACE_NAMES:
                    issues.append(
                        _new_issue(
                            "MCB-E130",
                            "ERROR",
                            geometry,
                            source_path,
                            face_path,
                            "Unknown cube face",
                            entity=entity,
                            bone_cube_path=cube_path,
                        )
                    )
                    continue
                if isinstance(face, (list, tuple)):
                    face_data = {"uv": face}
                elif isinstance(face, dict):
                    face_data = face
                else:
                    issues.append(
                        _new_issue(
                            "MCB-E130",
                            "ERROR",
                            geometry,
                            source_path,
                            face_path,
                            "Per-face UV entry must be an object or pair",
                            entity=entity,
                            bone_cube_path=cube_path,
                        )
                    )
                    continue
                _unknown_keys(
                    face_data,
                    SUPPORTED_UV_FACE_KEYS,
                    face_path,
                    issues,
                    geometry,
                    source_path,
                    entity,
                )
                if "uv" not in face_data:
                    issues.append(
                        _new_issue(
                            "MCB-E130",
                            "ERROR",
                            geometry,
                            source_path,
                            face_path,
                            "Per-face UV requires an uv origin",
                            entity=entity,
                            bone_cube_path=cube_path,
                        )
                    )
                else:
                    _validate_pair(
                        face_data["uv"],
                        f"{face_path}.uv",
                        issues,
                        geometry,
                        source_path,
                        entity,
                    )
                    default_size = None
                    if size_valid:
                        sx, sy, sz = (float(value) for value in size[:3])
                        if face_name in {"up", "down"}:
                            default_size = (sx, sz)
                        elif face_name in {"east", "west"}:
                            default_size = (sz, sy)
                        else:
                            default_size = (sx, sy)
                    face_size = face_data.get("uv_size", default_size)
                    if "uv_size" in face_data:
                        _validate_pair(
                            face_data["uv_size"],
                            f"{face_path}.uv_size",
                            issues,
                            geometry,
                            source_path,
                            entity,
                        )
                    # Faces the importer never builds (no area) cannot show
                    # their UVs, wherever those point.
                    built = True
                    if size_valid:
                        inflate = cube.get("inflate", bone.get("inflate", 0))
                        inflate = (
                            float(inflate)
                            if isinstance(inflate, (int, float))
                            and not isinstance(inflate, bool)
                            and math.isfinite(float(inflate))
                            else 0.0
                        )
                        built = per_face_uv_is_built(
                            uv, face_name,
                            *(abs(value) + 2.0 * inflate for value in (sx, sy, sz)),
                        )
                    if face_size is not None and built:
                        _validate_uv_bounds(
                            face_data["uv"],
                            face_size,
                            texture_width,
                            texture_height,
                            face_path,
                            issues,
                            geometry,
                            source_path,
                            entity,
                            cube_path,
                        )
                _validate_uv_rotation(
                    face_data,
                    face_path,
                    schema_family,
                    issues,
                    geometry,
                    source_path,
                    entity,
                )
    return issues


def build_import_report(
    geometry,
    source_path="",
    entity="",
    created_datablocks=(),
    source_provenance=(),
):
    return ImportReport(
        issues=tuple(
            inspect_geometry_visuals(
                geometry,
                source_path=source_path,
                entity=entity,
            )
        ),
        created_datablocks=tuple(created_datablocks),
        source_provenance=tuple(source_provenance),
    )


def raise_for_unsupported_visuals(
    geometry,
    source_path="",
    entity="",
):
    report = build_import_report(
        geometry,
        source_path=source_path,
        entity=entity,
    )
    if not report.fatal:
        return report
    identifier = _geometry_identifier(geometry) or "unknown"
    format_version = (
        geometry.get("_mcbedrock_format_version", "unknown")
        if isinstance(geometry, dict)
        else "unknown"
    )
    raise VisualCompatibilityError(
        identifier,
        format_version,
        report.fatal,
        report=report,
    )

