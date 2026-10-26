"""Blockbench ``.bbmodel`` parsing and loss-aware Bedrock conversion.

The project format is intentionally broader than Minecraft geometry JSON.  The
pure helpers in this module keep file/version handling testable without
Blender; the Blender-facing builder consumes the converted geometry, embedded
textures, locators, and animation payload.
"""

from __future__ import annotations

import json
import math
import os
import re


def load_bbmodel(filepath):
    with open(filepath, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("meta"), dict):
        raise ValueError("Not a valid Blockbench .bbmodel project")
    return data


def _number(value, default=0.0):
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else float(default)
        except ValueError:
            return float(default)
    return float(default)


def _vector(value, default=(0.0, 0.0, 0.0)):
    if isinstance(value, dict):
        value = [value.get("x"), value.get("y"), value.get("z")]
    if not isinstance(value, (list, tuple)):
        return [float(component) for component in default]
    result = [
        _number(value[index], default[index])
        if index < len(value) else float(default[index])
        for index in range(3)
    ]
    return result


def _bedrock_point(value, before_v5):
    """Convert Blockbench 5 editor coordinates to Bedrock model space."""
    result = _vector(value)
    if not before_v5:
        result[0] *= -1.0
    return result


def _bedrock_rotation(value, before_v5):
    """Convert Blockbench 5 editor Euler values to Bedrock model space."""
    result = _vector(value)
    if not before_v5:
        result[0] *= -1.0
        result[1] *= -1.0
    return result


def _safe_name(value, fallback):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("_")
    return value or fallback


def _unique_name(value, fallback, used):
    base = _safe_name(value, fallback)
    candidate = base
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def animation_items(filepath_or_data):
    """Return ``(identifier, display name, description)`` enum choices."""
    data = (
        load_bbmodel(filepath_or_data)
        if isinstance(filepath_or_data, (str, os.PathLike))
        else filepath_or_data
    )
    result = [("__NONE__", "No animation", "Import the model at its authored rest pose")]
    animations = data.get("animations", []) if isinstance(data, dict) else []
    seen = set()
    for index, animation in enumerate(animations if isinstance(animations, list) else []):
        if not isinstance(animation, dict):
            continue
        identifier = str(animation.get("uuid") or animation.get("name") or f"animation_{index}")
        if identifier in seen:
            continue
        seen.add(identifier)
        name = str(animation.get("name") or f"Animation {index + 1}")
        result.append((identifier, name, str(animation.get("path") or name)))
    if seen:
        result.append((
            "__ALL__",
            "All animations",
            "Import every Blockbench clip and arrange them as playable NLA tracks",
        ))
    return result


def _format_version(data):
    meta = data.get("meta", {}) if isinstance(data, dict) else {}
    return str(meta.get("format_version", meta.get("format", "0")) or "0")


def _version_before_5(data):
    try:
        return float(_format_version(data).split(".", 2)[0] + "." + _format_version(data).split(".", 2)[1]) < 5.0
    except (IndexError, TypeError, ValueError):
        return True


def _group_sources(data):
    groups = {}
    for group in data.get("groups", []) if isinstance(data.get("groups"), list) else []:
        if isinstance(group, dict) and group.get("uuid"):
            groups[str(group["uuid"])] = group

    def collect(items):
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            uuid = item.get("uuid")
            if uuid and ("name" in item or "origin" in item or "rotation" in item):
                merged = dict(groups.get(str(uuid), {}))
                merged.update(item)
                groups[str(uuid)] = merged
            collect(item.get("children", []))

    collect(data.get("outliner", []))
    return groups


def _cube_face(direction, face):
    if not isinstance(face, dict):
        return None
    if "texture" in face and face["texture"] is None:
        # Blockbench's "transparent" face: disabled, never rendered.
        return None
    uv = face.get("uv")
    if not isinstance(uv, (list, tuple)) or len(uv) < 4:
        return None
    u1, v1, u2, v2 = (_number(value) for value in uv[:4])
    if direction in ("up", "down"):
        # Blockbench's Bedrock codec stores up/down per-face UVs from the
        # opposite corner (uv += size, size *= -1), for every model.
        result = {
            "uv": [u2, v2],
            "uv_size": [u1 - u2, v1 - v2],
        }
    else:
        result = {
            "uv": [u1, v1],
            "uv_size": [u2 - u1, v2 - v1],
        }
    rotation = int(_number(face.get("rotation"), 0.0)) % 360
    if rotation:
        result["uv_rotation"] = rotation
    if "texture" in face:
        result["_bbmodel_texture"] = face.get("texture")
    return result


def _cube_definition(element, before_v5, float_box_uv=False):
    start = _vector(element.get("from"))
    end = _vector(element.get("to"))
    if not before_v5:
        start[0], end[0] = -end[0], -start[0]
    cube = {
        "origin": start,
        "size": [end[index] - start[index] for index in range(3)],
        "pivot": _bedrock_point(element.get("origin"), before_v5),
        "rotation": _bedrock_rotation(element.get("rotation"), before_v5),
        "_bbmodel_uuid": str(element.get("uuid", "")),
        "_bbmodel_name": str(element.get("name", "")),
    }
    if element.get("inflate") is not None:
        cube["inflate"] = _number(element.get("inflate"))
    if element.get("mirror_uv") is not None:
        cube["mirror"] = bool(element.get("mirror_uv"))
    faces = {}
    for direction, face in (
        element.get("faces", {}).items()
        if isinstance(element.get("faces"), dict) else ()
    ):
        converted = _cube_face(str(direction), face)
        if converted is not None:
            faces[str(direction)] = converted
    if faces:
        cube["uv"] = faces
    elif isinstance(element.get("uv_offset"), (list, tuple)):
        cube["uv"] = list(element["uv_offset"][:2])
        if float_box_uv:
            # Java Edition entity projects keep fractional box-UV sizes.
            cube["_mcbedrock_box_uv_float_size"] = True
    return cube


def _mesh_definition(element, before_v5):
    vertices = element.get("vertices", {})
    faces = element.get("faces", {})
    if not isinstance(vertices, dict) or not isinstance(faces, dict):
        return None
    positions = []
    uvs = []
    polygons = []
    for face in faces.values():
        if not isinstance(face, dict):
            continue
        refs = face.get("vertices", [])
        if not isinstance(refs, list) or len(refs) < 3:
            continue
        polygon = []
        face_uvs = face.get("uv", {})
        for vertex_id in refs:
            point = vertices.get(str(vertex_id))
            if not isinstance(point, (list, tuple)):
                continue
            position_index = len(positions)
            positions.append(_bedrock_point(point, before_v5))
            uv = (
                face_uvs.get(str(vertex_id), (0.0, 0.0))
                if isinstance(face_uvs, dict) else (0.0, 0.0)
            )
            uv_index = len(uvs)
            uvs.append([
                _number(uv[0]) if isinstance(uv, (list, tuple)) and uv else 0.0,
                _number(uv[1]) if isinstance(uv, (list, tuple)) and len(uv) > 1 else 0.0,
            ])
            polygon.append([position_index, position_index, uv_index])
        if not before_v5:
            polygon.reverse()
        if len(polygon) >= 3:
            # Bedrock poly meshes support triangles/quads. Fan triangulation
            # retains every n-gon authored by generic Blockbench formats.
            if len(polygon) <= 4:
                polygons.append(polygon)
            else:
                polygons.extend([
                    [polygon[0], polygon[index], polygon[index + 1]]
                    for index in range(1, len(polygon) - 1)
                ])
    if not polygons:
        return None
    return {
        "positions": positions,
        "normals": [],
        "uvs": uvs,
        "polys": polygons,
        "normalized_uvs": False,
    }


def _texture_size(data):
    resolution = data.get("resolution", {})
    if not isinstance(resolution, dict):
        resolution = {}
    textures = data.get("textures", [])
    first = textures[0] if isinstance(textures, list) and textures else {}
    first = first if isinstance(first, dict) else {}
    return (
        max(1.0, _number(first.get("uv_width", resolution.get("width", 16)), 16)),
        max(1.0, _number(first.get("uv_height", resolution.get("height", 16)), 16)),
    )


def convert_geometry(data, source_name="Blockbench_Model"):
    """Convert cubes, meshes, groups, locators and nulls to Bedrock geometry."""
    before_v5 = _version_before_5(data)
    # Blockbench keeps fractional box-UV sizes only for this format.
    float_box_uv = (
        str(data.get("meta", {}).get("model_format", "")) == "modded_entity"
    )
    elements = {
        str(element.get("uuid")): element
        for element in data.get("elements", data.get("cubes", []))
        if isinstance(element, dict) and element.get("uuid")
    }
    groups = _group_sources(data)
    used_names = set()
    bone_by_group = {}
    bones = []
    assignments = {}

    root_name = _unique_name("model_root", "model_root", used_names)
    root_bone = {"name": root_name, "pivot": [0.0, 0.0, 0.0], "cubes": [], "locators": {}}
    bones.append(root_bone)

    def ensure_group(uuid, parent_name):
        uuid = str(uuid)
        if uuid in bone_by_group:
            name = bone_by_group[uuid]
            bone = next(item for item in bones if item["name"] == name)
            if parent_name and not bone.get("parent") and name != root_name:
                bone["parent"] = parent_name
            return name
        source = groups.get(uuid, {})
        name = _unique_name(source.get("name"), "group", used_names)
        bone = {
            "name": name,
            "pivot": _bedrock_point(source.get("origin"), before_v5),
            "rotation": _bedrock_rotation(source.get("rotation"), before_v5),
            "cubes": [],
            "locators": {},
            "_bbmodel_uuid": uuid,
        }
        if parent_name:
            bone["parent"] = parent_name
        bones.append(bone)
        bone_by_group[uuid] = name
        return name

    def walk(items, parent_name):
        for item in items if isinstance(items, list) else []:
            if isinstance(item, str):
                if item in elements:
                    assignments[item] = parent_name
                elif item in groups:
                    ensure_group(item, parent_name)
                continue
            if not isinstance(item, dict):
                continue
            uuid = str(item.get("uuid", ""))
            is_group = bool(item.get("children") is not None) or uuid in groups
            if is_group:
                group_name = ensure_group(uuid, parent_name)
                walk(item.get("children", []), group_name)
            elif uuid in elements:
                assignments[uuid] = parent_name

    walk(data.get("outliner", []), root_name)
    bones_by_name = {bone["name"]: bone for bone in bones}

    for uuid, element in elements.items():
        parent = assignments.get(uuid, root_name)
        parent_bone = bones_by_name[parent]
        element_type = str(element.get("type") or "").lower()
        if not element_type and "from" in element and "to" in element:
            element_type = "cube"
        if element_type == "cube":
            parent_bone["cubes"].append(
                _cube_definition(element, before_v5, float_box_uv)
            )
        elif element_type == "mesh":
            poly_mesh = _mesh_definition(element, before_v5)
            if poly_mesh:
                mesh_name = _unique_name(element.get("name"), "mesh", used_names)
                mesh_bone = {
                    "name": mesh_name,
                    "parent": parent,
                    "pivot": _bedrock_point(element.get("origin"), before_v5),
                    "rotation": _bedrock_rotation(element.get("rotation"), before_v5),
                    "poly_mesh": poly_mesh,
                    "_bbmodel_uuid": uuid,
                }
                bones.append(mesh_bone)
                bones_by_name[mesh_name] = mesh_bone
        elif element_type in {"locator", "null"}:
            locator_name = _unique_name(element.get("name"), element_type or "locator", used_names)
            parent_bone.setdefault("locators", {})[locator_name] = {
                "offset": _bedrock_point(
                    element.get("position", element.get("from", element.get("origin"))),
                    before_v5,
                ),
                "rotation": _bedrock_rotation(
                    element.get("rotation"), before_v5),
                "_bbmodel_uuid": uuid,
                "_bbmodel_type": element_type,
            }
        else:
            locator_name = _unique_name(element.get("name"), element_type or "element", used_names)
            parent_bone.setdefault("locators", {})[locator_name] = {
                "offset": _bedrock_point(
                    element.get("position", element.get("origin")), before_v5),
                "rotation": _bedrock_rotation(
                    element.get("rotation"), before_v5),
                "_bbmodel_uuid": uuid,
                "_bbmodel_type": element_type or "unknown",
            }

    width, height = _texture_size(data)
    identifier = str(
        data.get("model_identifier")
        or data.get("geometry_name")
        or source_name
    )
    if not identifier.startswith("geometry."):
        identifier = "geometry." + _safe_name(identifier, "bbmodel")
    return {
        "description": {
            "identifier": identifier,
            "texture_width": width,
            "texture_height": height,
        },
        "bones": bones,
        "_mcbedrock_format_version": "1.21.0",
        "_mcbedrock_schema_family": "bbmodel",
    }, bone_by_group


def _keyframe_value(keyframe, channel, before_v5):
    points = keyframe.get("data_points", [])
    point = points[0] if isinstance(points, list) and points else {}
    value = _vector(point)
    # Blockbench project format 5 stores animation keyframes in editor space.
    # Bedrock animation JSON uses the opposite X direction for position and
    # the opposite X/Y rotation directions. Pre-v5 projects already store the
    # Bedrock-space values; Blockbench itself applies this same migration only
    # while loading those old projects into its v5 editor representation.
    if not before_v5:
        if channel in {"position", "rotation"}:
            value[0] *= -1.0
        if channel == "rotation":
            value[1] *= -1.0
    return value


def convert_animations(data, bone_by_group):
    """Return a Bedrock animation JSON payload keyed by Blockbench clip name."""
    animations = {}
    animation_ids = {}
    before_v5 = _version_before_5(data)
    for index, source in enumerate(
        data.get("animations", []) if isinstance(data.get("animations"), list) else []
    ):
        if not isinstance(source, dict):
            continue
        name = str(source.get("name") or f"animation.bbmodel.{index + 1}")
        identifier = name if name.startswith("animation.") else f"animation.{_safe_name(name, f'clip_{index + 1}')}"
        suffix = 2
        base = identifier
        while identifier in animations:
            identifier = f"{base}_{suffix}"
            suffix += 1
        clip = {
            "loop": source.get("loop"),
            "animation_length": max(0.0, _number(source.get("length"), 0.0)),
            "bones": {},
        }
        animators = source.get("animators", {})
        for group_uuid, animator in animators.items() if isinstance(animators, dict) else ():
            if not isinstance(animator, dict):
                continue
            bone_name = bone_by_group.get(str(group_uuid))
            if not bone_name:
                continue
            channels = {}
            by_channel = {}
            for keyframe in animator.get("keyframes", []) if isinstance(animator.get("keyframes"), list) else []:
                if not isinstance(keyframe, dict):
                    continue
                channel = str(keyframe.get("channel") or "").lower()
                if channel not in {"position", "rotation", "scale"}:
                    continue
                time = max(0.0, _number(keyframe.get("time"), 0.0))
                value = _keyframe_value(keyframe, channel, before_v5)
                interpolation = str(keyframe.get("interpolation") or "linear").lower()
                if interpolation == "step":
                    encoded = {"pre": value, "post": value, "lerp_mode": "linear"}
                elif interpolation in {"catmullrom", "catmull_rom"}:
                    encoded = {"post": value, "lerp_mode": "catmullrom"}
                else:
                    encoded = value
                by_channel.setdefault(channel, {})[f"{time:.8g}"] = encoded
                clip["animation_length"] = max(clip["animation_length"], time)
            for channel, values in by_channel.items():
                channels[channel] = values
            if channels:
                clip["bones"][bone_name] = channels
        animations[identifier] = clip
        animation_ids[str(source.get("uuid") or source.get("name") or index)] = identifier
    return {"format_version": "1.8.0", "animations": animations}, animation_ids


def project_summary(data):
    elements = data.get("elements", data.get("cubes", []))
    elements = elements if isinstance(elements, list) else []
    counts = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        kind = str(element.get("type") or ("cube" if "from" in element else "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "format_version": _format_version(data),
        "model_format": str(data.get("meta", {}).get("model_format", "")),
        "element_counts": counts,
        "texture_count": len(data.get("textures", [])) if isinstance(data.get("textures"), list) else 0,
        "animation_count": len(data.get("animations", [])) if isinstance(data.get("animations"), list) else 0,
    }
