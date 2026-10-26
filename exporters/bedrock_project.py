"""Convert native Bedrock geometry JSON into a Blockbench 5 project."""

from __future__ import annotations

import os
import uuid


def _number(value):
    value = round(float(value), 6)
    return int(value) if value.is_integer() else value


def _vector(value):
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    return [float(value[index]) if index < len(value) else 0.0 for index in range(3)]


def _editor_point(value):
    x, y, z = _vector(value)
    return [_number(-x), _number(y), _number(z)]


def _editor_rotation(value):
    x, y, z = _vector(value)
    return [_number(-x), _number(-y), _number(z)]


def _stable_uuid(namespace, *parts):
    seed = "\x1f".join(str(part) for part in (namespace, *parts))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "mcbedrockparticles:" + seed))


def _face(face):
    if not isinstance(face, dict):
        return None
    uv = face.get("uv")
    if not isinstance(uv, (list, tuple)) or len(uv) < 2:
        return None
    size = face.get("uv_size", (0.0, 0.0))
    size = size if isinstance(size, (list, tuple)) else (0.0, 0.0)
    u, v = float(uv[0]), float(uv[1])
    width = float(size[0]) if size else 0.0
    height = float(size[1]) if len(size) > 1 else 0.0
    result = {
        "uv": [_number(u), _number(v), _number(u + width), _number(v + height)],
        "texture": 0,
    }
    try:
        rotation = int(float(face.get("rotation", 0))) % 360
    except (TypeError, ValueError):
        rotation = 0
    if rotation:
        result["rotation"] = rotation
    return result


def _cube(cube, bone, namespace, index):
    origin = _vector(cube.get("origin"))
    size = _vector(cube.get("size"))
    cube_id = _stable_uuid(namespace, "cube", bone.get("name", "bone"), index)
    result = {
        "type": "cube",
        "uuid": cube_id,
        "name": str(cube.get("_bbmodel_name") or f"{bone.get('name', 'bone')} cube {index + 1}"),
        "from": [
            _number(-(origin[0] + size[0])),
            _number(origin[1]),
            _number(origin[2]),
        ],
        "to": [
            _number(-origin[0]),
            _number(origin[1] + size[1]),
            _number(origin[2] + size[2]),
        ],
        "origin": _editor_point(cube.get("pivot", bone.get("pivot"))),
        "rotation": _editor_rotation(cube.get("rotation")),
        "visibility": True,
        "export": True,
    }
    if cube.get("inflate") is not None:
        result["inflate"] = _number(cube["inflate"])
    if cube.get("mirror") is not None:
        result["mirror_uv"] = bool(cube["mirror"])
    cube_uv = cube.get("uv")
    if isinstance(cube_uv, dict):
        result["faces"] = {
            str(direction): converted
            for direction, value in cube_uv.items()
            if (converted := _face(value)) is not None
        }
    elif isinstance(cube_uv, (list, tuple)) and len(cube_uv) >= 2:
        result["uv_offset"] = [_number(cube_uv[0]), _number(cube_uv[1])]
    return result


def _mesh(poly_mesh, bone, namespace):
    if not isinstance(poly_mesh, dict):
        return None
    positions = poly_mesh.get("positions", [])
    polygons = poly_mesh.get("polys", [])
    uvs = poly_mesh.get("uvs", [])
    if not isinstance(positions, list) or not isinstance(polygons, list):
        return None
    vertices = {
        f"v{index}": _editor_point(position)
        for index, position in enumerate(positions)
        if isinstance(position, (list, tuple))
    }
    faces = {}
    for face_index, polygon in enumerate(polygons):
        if not isinstance(polygon, list) or len(polygon) < 3:
            continue
        refs, face_uv = [], {}
        for corner in reversed(polygon):
            if not isinstance(corner, (list, tuple)) or not corner:
                continue
            vertex_id = f"v{int(corner[0])}"
            if vertex_id not in vertices:
                continue
            refs.append(vertex_id)
            uv_index = int(corner[2]) if len(corner) > 2 else -1
            if 0 <= uv_index < len(uvs) and isinstance(uvs[uv_index], (list, tuple)):
                face_uv[vertex_id] = [
                    _number(uvs[uv_index][0]),
                    _number(uvs[uv_index][1] if len(uvs[uv_index]) > 1 else 0),
                ]
        if len(refs) >= 3:
            faces[f"f{face_index}"] = {"vertices": refs, "uv": face_uv, "texture": 0}
    if not faces:
        return None
    return {
        "type": "mesh",
        "uuid": _stable_uuid(namespace, "mesh", bone.get("name", "bone")),
        "name": str(bone.get("name") or "mesh"),
        "origin": _editor_point(bone.get("pivot")),
        "rotation": _editor_rotation(bone.get("rotation")),
        "vertices": vertices,
        "faces": faces,
        "visibility": True,
        "export": True,
    }


def bedrock_geometry_project(geometry, *, source_path="", texture_path="", project_path=""):
    """Create a stable, re-exportable Blockbench project from Bedrock geometry."""
    if not isinstance(geometry, dict):
        raise ValueError("Bedrock geometry is not an object")
    description = geometry.get("description", {})
    description = description if isinstance(description, dict) else {}
    identifier = str(description.get("identifier") or "geometry.model")
    namespace = os.path.abspath(source_path) + "#" + identifier
    bones = [
        bone for bone in geometry.get("bones", [])
        if isinstance(bone, dict) and bone.get("name")
    ]
    group_ids = {
        str(bone["name"]): _stable_uuid(namespace, "bone", bone["name"])
        for bone in bones
    }
    elements = []
    own_children = {str(bone["name"]): [] for bone in bones}
    groups = []
    for bone in bones:
        bone_name = str(bone["name"])
        groups.append({
            "uuid": group_ids[bone_name],
            "name": bone_name,
            "origin": _editor_point(bone.get("pivot")),
            "rotation": _editor_rotation(bone.get("rotation")),
            "visibility": True,
            "export": True,
        })
        cubes = bone.get("cubes", []) if isinstance(bone.get("cubes"), list) else []
        for cube_index, source_cube in enumerate(cubes):
            if not isinstance(source_cube, dict):
                continue
            element = _cube(source_cube, bone, namespace, cube_index)
            elements.append(element)
            own_children[bone_name].append(element["uuid"])
        mesh = _mesh(bone.get("poly_mesh"), bone, namespace)
        if mesh is not None:
            elements.append(mesh)
            own_children[bone_name].append(mesh["uuid"])
        locators = bone.get("locators", {})
        for locator_index, (locator_name, locator) in enumerate(
            locators.items() if isinstance(locators, dict) else ()
        ):
            locator = locator if isinstance(locator, dict) else {"offset": locator}
            locator_id = _stable_uuid(namespace, "locator", bone_name, locator_name, locator_index)
            elements.append({
                "type": "locator",
                "uuid": locator_id,
                "name": str(locator_name),
                "position": _editor_point(locator.get("offset")),
                "rotation": _editor_rotation(locator.get("rotation")),
                "visibility": True,
                "export": True,
            })
            own_children[bone_name].append(locator_id)

    nested = {}
    for bone in reversed(bones):
        bone_name = str(bone["name"])
        nested[bone_name] = {
            "uuid": group_ids[bone_name],
            "name": bone_name,
            "children": own_children[bone_name] + [
                nested[str(child["name"])]
                for child in bones
                if str(child.get("parent", "")).casefold() == bone_name.casefold()
                and str(child["name"]) in nested
            ],
        }
    bone_names = {str(bone["name"]).casefold() for bone in bones}
    roots = [
        nested[str(bone["name"])] for bone in bones
        if str(bone.get("parent", "")).casefold() not in bone_names
    ]
    try:
        width = max(1, int(float(description.get("texture_width", 64))))
        height = max(1, int(float(description.get("texture_height", 64))))
    except (TypeError, ValueError):
        width, height = 64, 64
    project = {
        "meta": {
            "format_version": "5.0",
            "model_format": "bedrock",
            "box_uv": any(
                isinstance(cube.get("uv"), (list, tuple))
                for bone in bones for cube in bone.get("cubes", [])
                if isinstance(cube, dict)
            ),
        },
        "name": identifier.rsplit(".", 1)[-1] or "model",
        "model_identifier": identifier.removeprefix("geometry."),
        "resolution": {"width": width, "height": height},
        "elements": elements,
        "groups": groups,
        "outliner": roots,
        "textures": [],
        "animations": [],
        "mcbedrockparticles_source_geometry": os.path.abspath(source_path),
    }
    if texture_path:
        absolute = os.path.abspath(texture_path)
        project_dir = os.path.dirname(os.path.abspath(project_path)) if project_path else ""
        try:
            relative = os.path.relpath(absolute, project_dir) if project_dir else ""
        except ValueError:
            relative = ""
        project["textures"] = [{
            "uuid": _stable_uuid(namespace, "texture", absolute),
            "name": os.path.basename(absolute),
            "path": absolute,
            "relative_path": relative,
            "uv_width": width,
            "uv_height": height,
        }]
    return project

_base_bedrock_geometry_project = globals().get(
    "_base_bedrock_geometry_project", bedrock_geometry_project
)


def _hidden_bones(geometry, hidden_parts):
    """Resolve renderer visibility patterns through the complete bone tree."""
    from fnmatch import fnmatchcase

    bones = [
        bone for bone in geometry.get("bones", ())
        if isinstance(bone, dict) and bone.get("name")
    ]
    by_lower = {str(bone["name"]).casefold(): bone for bone in bones}
    patterns = tuple(str(pattern).casefold() for pattern in hidden_parts or ())
    visible = tuple(pattern[1:] for pattern in patterns if pattern.startswith("!"))
    hidden = tuple(pattern for pattern in patterns if not pattern.startswith("!"))
    result = set()
    for bone in bones:
        name = str(bone["name"])
        current = bone
        seen = set()
        while current is not None:
            current_name = str(current.get("name", ""))
            folded = current_name.casefold()
            if not folded or folded in seen:
                break
            seen.add(folded)
            if bool(current.get(
                "neverRender", current.get("never_render", False)
            )):
                result.add(name)
                break
            if any(fnmatchcase(folded, pattern) for pattern in visible):
                break
            if any(fnmatchcase(folded, pattern) for pattern in hidden):
                result.add(name)
                break
            current = by_lower.get(str(current.get("parent", "")).casefold())
    return result


def bedrock_geometry_project(
    geometry,
    *,
    source_path="",
    texture_path="",
    project_path="",
    hidden_parts=(),
):
    """Create a project from only the geometry rendered by the imported rig."""
    project = _base_bedrock_geometry_project(
        geometry,
        source_path=source_path,
        texture_path=texture_path,
        project_path=project_path,
    )
    hidden_names = _hidden_bones(geometry, hidden_parts)
    if not hidden_names:
        return project

    hidden_folded = {name.casefold() for name in hidden_names}
    group_ids = {
        str(group.get("uuid")): str(group.get("name", ""))
        for group in project.get("groups", ())
        if isinstance(group, dict) and group.get("uuid")
    }
    hidden_elements = set()

    def filter_nodes(nodes, inherited_hidden=False):
        for node in nodes if isinstance(nodes, list) else ():
            if not isinstance(node, dict):
                continue
            name = str(node.get("name") or group_ids.get(str(node.get("uuid")), ""))
            node_hidden = inherited_hidden or name.casefold() in hidden_folded
            children = node.get("children", [])
            if not isinstance(children, list):
                continue
            kept = []
            for child in children:
                if isinstance(child, dict):
                    filter_nodes([child], node_hidden)
                    kept.append(child)
                elif node_hidden:
                    hidden_elements.add(str(child))
                else:
                    kept.append(child)
            node["children"] = kept

    filter_nodes(project.get("outliner", []))
    project["elements"] = [
        element for element in project.get("elements", [])
        if str(element.get("uuid", "")) not in hidden_elements
    ]
    for group in project.get("groups", []):
        if str(group.get("name", "")).casefold() in hidden_folded:
            group["visibility"] = False
            group["export"] = False
    return project
