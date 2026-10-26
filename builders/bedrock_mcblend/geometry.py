"""MCblend-inspired Bedrock geometry builder."""

import json
import os
from copy import deepcopy
from types import SimpleNamespace

import bpy
from mathutils import Euler, Matrix, Vector

from ..model_origin import place_transform_origin_below_model
from ..selection import deselect_all_objects
from ...importers.bedrock_assets import safe_name
from ..bone_math import (
    bone_absolute_matrix,
    bedrock_space_rotation_matrix,
    bone_rotation,
    bone_rest_world_matrix,
    topological_bone_names,
    transform_cube_vertex,
)
from ..bedrock_uv import (
    blender_winding,
    FACE_INDICES,
    build_cube_faces,
    mark_see_through_faces,
    uv_corners_are_valid,
    rect_uv,
    coplanar_lift_offsets,
)
from .materials import make_model_material
from .render_controller import part_is_hidden
from ...importers.bedrock_compatibility import texture_mesh_skipped_issue
from .uv import EPSILON, coerce_float, coerce_vector


def _matrix_from_rows(rows):
    return Matrix(rows)


def bedrock_to_blender(point):
    x, y, z = coerce_vector(point)
    return Vector((x, z, y)) / 16.0


def bedrock_rotation_to_euler(rotation):
    from ..bone_math import bedrock_rotation_to_euler_components

    return Euler(bedrock_rotation_to_euler_components(rotation), "XZY")


def _bone_bind_matrix(bone_name, bones_by_name, cache):
    """Armature bind matrix matching the transform baked into mesh vertices."""
    return _matrix_from_rows(bone_absolute_matrix(bone_name, bones_by_name, cache))


def _bone_rest_world_matrix(bone_name, bones_by_name, cache):
    """Armature rest matrix using absolute Bedrock model-space pivots."""
    return _matrix_from_rows(bone_rest_world_matrix(bone_name, bones_by_name, cache))


def _freeze_json_value(value):
    """Hashable structural representation used for exact cube deduplication."""
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze_json_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    if isinstance(value, float):
        return round(value, 8)
    return value


def cube_signature(bone_name, cube):
    return (str(bone_name).lower(), _freeze_json_value(cube))


def normalize_signed_cube_geometry(cube, origin, size):
    """Canonicalize finite signed extents as reflected cube bounds."""
    normalized = deepcopy(cube) if isinstance(cube, dict) else {}
    canonical_origin = [float(value) for value in origin]
    canonical_size = [float(value) for value in size]
    reflected_axes = []
    for axis in range(3):
        if canonical_size[axis] >= 0.0:
            continue
        canonical_origin[axis] += canonical_size[axis]
        canonical_size[axis] = -canonical_size[axis]
        reflected_axes.append(axis)
    if reflected_axes and not isinstance(normalized.get("uv"), dict):
        normalized["mirror"] = bool(normalized.get("mirror", False)) ^ (
            len(reflected_axes) % 2 == 1
        )
    normalized["_mcbedrock_signed_size_axes"] = tuple(reflected_axes)
    return normalized, tuple(canonical_origin), tuple(canonical_size)

def effective_cube_overlap_properties(bone_name, cube, bones_by_name=None):
    """Effective owner-local inflate/mirror used only for overlap identity.

    Cube values override the owner bone. Bone defaults deliberately do not
    inherit through the parent chain.
    """
    cube = cube if isinstance(cube, dict) else {}
    bones_by_name = bones_by_name if isinstance(bones_by_name, dict) else {}
    owner = next((
        bone for name, bone in bones_by_name.items()
        if str(name).casefold() == str(bone_name).casefold()
        and isinstance(bone, dict)
    ), {})
    return {
        "inflate": coerce_float(cube.get("inflate", owner.get("inflate", 0.0))),
        "mirror": bool(cube.get("mirror", owner.get("mirror", False))),
    }


def cube_signature_key(bone_name, cube, bones_by_name=None):
    """Stable key for a cube plus its complete authored bone transform chain."""
    lineage = []
    seen = set()
    current = str(bone_name)
    bones_by_name = bones_by_name if isinstance(bones_by_name, dict) else {}
    while current and current.casefold() not in seen:
        seen.add(current.casefold())
        bone = bones_by_name.get(current, {})
        if not isinstance(bone, dict):
            bone = {}
        lineage.append((
            current.casefold(),
            _freeze_json_value({
                key: bone[key]
                for key in (
                    "parent", "pivot", "rotation", "bind_pose_rotation",
                    "_mcbedrock_native_reflected",
                    "_mcbedrock_native_parent_offset",
                    "_mcbedrock_native_template",
                )
                if key in bone
            }),
        ))
        current = str(bone.get("parent", ""))
    return json.dumps(
        (
            cube_signature(bone_name, cube),
            _freeze_json_value(effective_cube_overlap_properties(
                bone_name, cube, bones_by_name
            )),
            tuple(lineage),
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _merge_duplicate_bones(bones_data):
    """Collapse malformed legacy duplicate bone declarations by authored name."""
    merged = []
    positions = {}
    for bone in bones_data if isinstance(bones_data, list) else []:
        if not isinstance(bone, dict):
            continue
        name = str(bone.get("name", "bone"))
        key = name.casefold()
        if key not in positions:
            positions[key] = len(merged)
            merged.append(deepcopy(bone))
            continue
        combined = dict(merged[positions[key]])
        combined.update(deepcopy(bone))
        merged[positions[key]] = combined
    return merged


def _resolve_binding_parent(binding, available_names):
    """Resolve only an authored literal bone binding.

    Dynamic Molang binding expressions depend on runtime item/entity state.
    Guessing a target from semantic words silently rewrites the source
    hierarchy, so expressions remain recorded but unbound in static previews.
    """
    if not isinstance(binding, str) or not binding.strip():
        return ""
    literal = binding.strip()
    exact = {str(name): str(name) for name in available_names}
    if literal in exact:
        return exact[literal]
    return {name.casefold(): name for name in exact}.get(literal.casefold(), "")

def _resolve_literal_parent(parent, available_names, child_name=""):
    """Resolve authored parent names with Bedrock's case-insensitive matching."""
    if not isinstance(parent, str) or not parent.strip():
        return ""
    literal = parent.strip()
    resolved = {
        str(name).casefold(): str(name)
        for name in available_names
    }.get(literal.casefold(), literal)
    if child_name and resolved.casefold() == str(child_name).casefold():
        return ""
    return resolved



def _clean_import_name(name, fallback):
    raw = str(name or "").strip() or str(fallback or "minecraft_asset").strip()
    raw = raw.replace("\\", "_").replace("/", "_").replace(":", "_")
    raw = raw.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    raw = " ".join(raw.split())
    return raw or "minecraft_asset"


def _set_armature_display(arm_data, arm_obj):
    for display_type in ("OCTAHEDRAL", "STICK", "WIRE"):
        try:
            arm_data.display_type = display_type
            break
        except (TypeError, ValueError):
            continue
    if hasattr(arm_data, "show_names"):
        arm_data.show_names = True
    if hasattr(arm_obj, "display_type"):
        arm_obj.display_type = "WIRE"
    if hasattr(arm_obj, "show_in_front"):
        arm_obj.show_in_front = True


def _armature_control_length(bones_data):
    """Return a visible bone length without changing any bone orientation."""
    minimum = [float("inf")] * 3
    maximum = [float("-inf")] * 3
    found = False
    for bone_def in bones_data:
        if not isinstance(bone_def, dict):
            continue
        bone_inflate = coerce_float(bone_def.get("inflate", 0.0))
        for cube_index, cube in enumerate(bone_def.get("cubes", [])):
            if not isinstance(cube, dict):
                continue
            origin = coerce_vector(cube.get("origin", [0, 0, 0]))
            size = coerce_vector(cube.get("size", [0, 0, 0]))
            inflate = coerce_float(cube.get("inflate", bone_inflate))
            for axis in range(3):
                lo = min(origin[axis], origin[axis] + size[axis]) - inflate
                hi = max(origin[axis], origin[axis] + size[axis]) + inflate
                minimum[axis] = min(minimum[axis], lo)
                maximum[axis] = max(maximum[axis], hi)
            found = True
    if not found:
        return 0.15
    model_span = max(maximum[i] - minimum[i] for i in range(3)) / 16.0
    # Relative to the model, so small models get small bones; the floor only
    # keeps a bone from collapsing to zero length.
    return min(max(model_span * 0.125, 0.005), 0.5)


def _owned_bone_points(bone_def, bone_name, bones_by_name, bone_cache):
    """Return fully transformed bind-space points owned by one source bone."""
    points = []
    if not isinstance(bone_def, dict):
        return points
    bone_inflate = coerce_float(bone_def.get("inflate", 0.0))
    for cube in bone_def.get("cubes", []):
        if not isinstance(cube, dict):
            continue
        origin = coerce_vector(cube.get("origin", [0, 0, 0]))
        size = coerce_vector(cube.get("size", [0, 0, 0]))
        inflate = coerce_float(cube.get("inflate", bone_inflate))
        bounds = [
            (
                min(origin[axis], origin[axis] + size[axis]) - inflate,
                max(origin[axis], origin[axis] + size[axis]) + inflate,
            )
            for axis in range(3)
        ]
        for x in bounds[0]:
            for y in bounds[1]:
                for z in bounds[2]:
                    points.append(Vector(transform_cube_vertex(
                        (x, y, z), cube, bone_def, bone_name,
                        bones_by_name, bone_cache,
                    )))
    poly_mesh = bone_def.get("poly_mesh")
    if isinstance(poly_mesh, dict):
        for point in poly_mesh.get("positions", []):
            if isinstance(point, (list, tuple)) and len(point) >= 3:
                points.append(Vector(transform_cube_vertex(
                    point, {}, bone_def, bone_name, bones_by_name, bone_cache,
                )))
    return points


def _principal_geometry_target(head, points, preferred_axis, minimum_length):
    """Aim from the bone head through the dominant owned-geometry extent."""
    if not points:
        return None
    centroid = sum(points, Vector()) / len(points)
    covariance = Matrix(((0.0, 0.0, 0.0),) * 3)
    for point in points:
        delta = point - centroid
        for row in range(3):
            for column in range(3):
                covariance[row][column] += delta[row] * delta[column]
    seeds = (
        preferred_axis,
        Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)),
    )
    candidates = []
    for seed in seeds:
        axis = seed.copy()
        if axis.length <= 1.0e-5:
            continue
        axis.normalize()
        for _index in range(12):
            projected = covariance @ axis
            if projected.length <= 1.0e-8:
                break
            axis = projected.normalized()
        candidates.append((axis.dot(covariance @ axis), axis))

    centroid_direction = centroid - head
    principal_variance, principal_axis = max(
        candidates, key=lambda item: item[0]
    )
    total_variance = sum(covariance[axis][axis] for axis in range(3))
    # A displaced pivot does not describe a part's orientation: rotated
    # quadruped torsos are the canonical example. Prefer the principal mesh
    # axis whenever it is meaningfully stronger than an isotropic cube, and
    # retain the centroid direction for near-spherical controls.
    if (
        principal_variance > 1.0e-8
        and principal_variance * 3.0 >= total_variance * 1.15
    ):
        direction = principal_axis
    elif centroid_direction.length > 1.0e-5:
        direction = centroid_direction.normalized()
    elif preferred_axis.length > 1.0e-5:
        direction = preferred_axis.normalized()
    else:
        direction = principal_axis

    forward_extent = max(
        ((point - head).dot(direction) for point in points), default=0.0
    )
    backward_extent = max(
        ((point - head).dot(-direction) for point in points), default=0.0
    )
    if backward_extent > forward_extent + 1.0e-5:
        direction.negate()
    projections = [(point - head).dot(direction) for point in points]
    extent = max(projections, default=0.0)
    if extent <= 1.0e-5:
        direction.negate()
        projections = [(point - head).dot(direction) for point in points]
        extent = max(projections)
    if extent <= 1.0e-5:
        return None
    # Aim at the centre of the part's far end, so a bone whose pivot sits
    # beside its part still runs through it instead of parallel to it.
    from_centroid = [(point - centroid).dot(direction) for point in points]
    far = max(from_centroid)
    far_points = [
        point for point, value in zip(points, from_centroid)
        if value >= far - 1.0e-5
    ]
    target = sum(far_points, Vector()) / len(far_points)
    aim = target - head
    if aim.length <= 1.0e-5:
        return head + direction * max(float(minimum_length), extent)
    # End halfway through the part along that line: where the line enters
    # the part's box (the head itself when the pivot is inside) to its far end.
    unit = aim.normalized()
    low = [min(point[axis] for point in points) for axis in range(3)]
    high = [max(point[axis] for point in points) for axis in range(3)]
    entry = 0.0
    for axis in range(3):
        if abs(unit[axis]) > 1.0e-9:
            near = min(
                (low[axis] - head[axis]) / unit[axis],
                (high[axis] - head[axis]) / unit[axis],
            )
            entry = max(entry, near)
    length = (min(entry, aim.length) + aim.length) / 2.0
    return head + unit * max(float(minimum_length), length)


def _fit_edit_bone_tails(
    edit_bones, bones_data, bones_by_name, bone_cache,
    control_length, target_names=None,
):
    """Aim deform bones at joints/geometry with compact display lengths."""
    source = {
        str(bone.get("name", "")): bone
        for bone in bones_data if isinstance(bone, dict)
    }
    target_names = set(target_names) if target_names is not None else None
    for edit_bone in edit_bones:
        if target_names is not None and edit_bone.name not in target_names:
            continue
        if edit_bone.get("mcbedrock_synthetic_root", False):
            edit_bone.use_deform = False
            edit_bone["mcbedrock_tail_strategy"] = "synthetic_root"
            continue
        source_matrix = edit_bone.matrix.copy()
        source_y = source_matrix.to_3x3() @ Vector((0, 1, 0))
        source_y = source_y.normalized() if source_y.length > 1.0e-5 else Vector((0, 1, 0))
        children = [
            child for child in edit_bones
            if child.parent == edit_bone
            and (child.head - edit_bone.head).length > 1.0e-5
        ]
        points = _owned_bone_points(
            source.get(edit_bone.name), edit_bone.name,
            bones_by_name, bone_cache,
        )
        if points:
            # Aim at a child joint only when it lies on this bone's own
            # geometry; otherwise the bone would leave its part.
            pad = 0.02 * max(control_length, 1.0e-6) / 0.125
            low = Vector([min(point[axis] for point in points) - pad for axis in range(3)])
            high = Vector([max(point[axis] for point in points) + pad for axis in range(3)])
            children = [
                child for child in children
                if all(low[axis] <= child.head[axis] <= high[axis] for axis in range(3))
            ]
        minimum_length = control_length * 0.25
        target = None
        strategy = ""
        if len(children) == 1:
            target, strategy = children[0].head.copy(), "single_child"
        elif len(children) > 1 and edit_bone.parent is not None:
            incoming = edit_bone.head - edit_bone.parent.head
            if incoming.length > 1.0e-5:
                incoming.normalize()
                scored = sorted(
                    (
                        incoming.dot((child.head - edit_bone.head).normalized()),
                        child.name, child,
                    )
                    for child in children
                )
                best, runner = scored[-1], scored[-2]
                if best[0] > 0.25 and best[0] - runner[0] >= 0.15:
                    target, strategy = best[2].head.copy(), "branch_continuation"
        if target is None:
            target = _principal_geometry_target(
                edit_bone.head, points, source_y, minimum_length
            )
            if target is not None:
                strategy = "geometry_axis"
        if target is None and children:
            child = min(
                children,
                key=lambda item: ((item.head - edit_bone.head).length, item.name),
            )
            target, strategy = child.head.copy(), "nearest_child"
        if target is None or (target - edit_bone.head).length <= 1.0e-5:
            target = edit_bone.head + source_y * control_length
            strategy = "source_axis"
        target_vector = target - edit_bone.head
        # Point along the bone's own Bedrock axis nearest the target and keep
        # the roll on those axes. Blender scales a pose bone along its own
        # axes, so a diagonal bone would turn an authored non-uniform scale
        # (a stretched nose, a breathing body) into shear it cannot show.
        axes = [source_matrix.to_3x3().col[index].normalized() for index in range(3)]
        aim = target_vector.normalized()
        axis_index = max(range(3), key=lambda index: abs(aim.dot(axes[index])))
        direction = axes[axis_index] * (1.0 if aim.dot(axes[axis_index]) >= 0.0 else -1.0)
        if points:
            # Halfway through the part along that axis (from where it starts
            # ahead of the head), sized by the part itself.
            projections = [(point - edit_bone.head).dot(direction) for point in points]
            far = max(projections)
            length = (max(0.0, min(projections)) + far) / 2.0 if far > 1.0e-5 else 0.0
        else:
            length = min(target_vector.dot(direction), float(control_length))
        length = max(length, minimum_length)
        target = edit_bone.head + direction * length
        edit_bone.tail = target
        edit_bone.align_roll(axes[2] if axis_index != 2 else axes[1])
        edit_bone.use_connect = False
        edit_bone["mcbedrock_tail_strategy"] = strategy
        edit_bone["mcbedrock_tail_target"] = [float(value) for value in target]


def finalize_bedrock_rig(context, arm_obj):
    """Leave a Bedrock rig ready for Object-mode movement and Ctrl-Tab posing."""
    if not arm_obj or arm_obj.type != "ARMATURE":
        return
    if context.mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError:
            pass
    root = next((bone for bone in arm_obj.data.bones if bone.parent is None), None)
    for pose_bone in arm_obj.pose.bones:
        pose_bone.rotation_mode = "XZY"
        pose_bone.lock_location = (False, False, False)
        pose_bone.lock_rotation = (False, False, False)
        pose_bone.lock_scale = (False, False, False)
        selection_owner = pose_bone if hasattr(pose_bone, "select") else pose_bone.bone
        if hasattr(selection_owner, "select"):
            selection_owner.select = pose_bone.bone == root
    if root is not None:
        arm_obj.data.bones.active = root

    # Keep deformation meshes selectable for direct editing/deletion, but tag
    # their owning rig so the add-on's Object Mode G/R/S and Delete shortcuts
    # can provide whole-model behavior without hiding the mesh from A-select.
    arm_obj.show_in_front = True
    for child in arm_obj.children:
        if child.type == "MESH":
            child.hide_select = False
            child["mcbedrock_move_with_armature"] = arm_obj.name

    deselect_all_objects(context)
    arm_obj.select_set(True)
    context.view_layer.objects.active = arm_obj


def ensure_unique_collection(context, name):
    col_name = name
    if col_name in bpy.data.collections:
        i = 1
        while f"{col_name}.{i:03d}" in bpy.data.collections:
            i += 1
        col_name = f"{col_name}.{i:03d}"
    collection = bpy.data.collections.new(col_name)
    context.collection.children.link(collection)
    return collection


def _origin_offset_from_verts(verts):
    if not verts:
        return Vector((0.0, 0.0, 0.0))
    vectors = [Vector(v) for v in verts]
    min_x = min(v.x for v in vectors)
    max_x = max(v.x for v in vectors)
    min_y = min(v.y for v in vectors)
    max_y = max(v.y for v in vectors)
    min_z = min(v.z for v in vectors)
    return Vector(((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, min_z))




def recenter_bedrock_composite(context, arm_obj, policy="GROUNDED"):
    """Position a completed model without rewriting source-space data.

    The armature object receives one presentation transform after every
    renderer contribution exists. Mesh vertices, bind matrices, pivots, and
    animation channels stay in their authored coordinate system.
    """
    if not arm_obj or arm_obj.type != "ARMATURE":
        return Vector((0.0, 0.0, 0.0))

    collection_objects = (
        arm_obj.users_collection[0].all_objects
        if arm_obj.users_collection
        else arm_obj.children
    )
    meshes = [
        obj for obj in collection_objects
        if obj.type == "MESH"
        and (
            obj.parent == arm_obj
            or any(
                modifier.type == "ARMATURE" and modifier.object == arm_obj
                for modifier in obj.modifiers
            )
        )
    ]
    if not meshes:
        return Vector((0.0, 0.0, 0.0))

    world_to_armature = arm_obj.matrix_world.inverted_safe()
    transforms = {
        obj.name: world_to_armature @ obj.matrix_world
        for obj in meshes
    }
    points = [
        transforms[obj.name] @ vertex.co
        for obj in meshes
        for vertex in obj.data.vertices
    ]
    policy = str(policy or "GROUNDED").upper()
    arm_obj["mcbedrock_origin_policy"] = policy
    if policy == "AUTHORED":
        return Vector((0.0, 0.0, 0.0))
    if policy == "CENTERED":
        minimum = Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
        maximum = Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
        offset = (minimum + maximum) * 0.5
    else:
        offset = _origin_offset_from_verts(points)
    if offset.length <= EPSILON:
        return offset

    # Presentation is an object-space wrapper transform. Source vertices,
    # armature rest matrices, and animation coordinates remain untouched.
    arm_obj.matrix_world = arm_obj.matrix_world @ Matrix.Translation(-offset)
    arm_obj["mcbedrock_origin_offset"] = tuple(offset)
    arm_obj["mcbedrock_presentation_offset"] = tuple(offset)
    arm_obj["mcbedrock_presentation_transform"] = "OBJECT_SPACE"
    return offset

def _is_bone_hidden(name, bones_by_name, hidden_bones):
    return part_is_hidden(name, bones_by_name, hidden_bones)


def _collect_poly_mesh(
    poly_mesh,
    bone_name,
    bone_def,
    bones_by_name,
    bone_matrix_cache,
    tex_w,
    tex_h,
):
    """Expand a Bedrock indexed/list polygon mesh into split Blender loops."""
    if not isinstance(poly_mesh, dict):
        raise ValueError(f"Bone '{bone_name}' poly_mesh must be an object")
    positions = poly_mesh.get("positions", [])
    normals = poly_mesh.get("normals", [])
    uvs = poly_mesh.get("uvs", [])
    polys = poly_mesh.get("polys", [])
    if not all(isinstance(values, list) for values in (positions, normals, uvs)):
        raise ValueError(f"Bone '{bone_name}' poly_mesh positions/normals/uvs must be arrays")

    if isinstance(polys, str):
        width = {"tri_list": 3, "quad_list": 4}.get(polys)
        if width is None:
            raise ValueError(f"Bone '{bone_name}' has unknown poly_mesh list '{polys}'")
        if len(positions) % width:
            raise ValueError(
                f"Bone '{bone_name}' poly_mesh {polys} has an incomplete polygon"
            )
        polygon_refs = [
            [(index, index, index) for index in range(start, start + width)]
            for start in range(0, len(positions), width)
        ]
    elif isinstance(polys, list):
        polygon_refs = []
        for polygon in polys:
            if not isinstance(polygon, list) or len(polygon) not in (3, 4):
                raise ValueError(
                    f"Bone '{bone_name}' poly_mesh polygons must be triangles or quads"
                )
            refs = []
            for element in polygon:
                if not isinstance(element, (list, tuple)) or not element:
                    raise ValueError(
                        f"Bone '{bone_name}' poly_mesh index must be an array"
                    )
                position_index = int(element[0])
                if len(element) > 2:
                    normal_index = int(element[1])
                    uv_index = int(element[2])
                else:
                    # Retain the older two-index position/UV extension while
                    # treating schema-compliant triples as position/normal/UV.
                    normal_index = position_index
                    uv_index = int(element[1]) if len(element) > 1 else position_index
                refs.append((position_index, normal_index, uv_index))
            polygon_refs.append(refs)
    else:
        raise ValueError(f"Bone '{bone_name}' poly_mesh polys must be an array or list mode")

    normalized = bool(poly_mesh.get("normalized_uvs", False))
    mesh_verts = []
    mesh_faces = []
    mesh_uvs = []
    mesh_normals = []
    for refs in polygon_refs:
        local_indices = []
        local_uvs = []
        for position_index, normal_index, uv_index in refs:
            try:
                point = positions[position_index]
            except (IndexError, TypeError):
                raise ValueError(
                    f"Bone '{bone_name}' poly_mesh position index {position_index} is invalid"
                ) from None
            transformed = transform_cube_vertex(
                point, {}, bone_def, bone_name, bones_by_name, bone_matrix_cache
            )
            local_indices.append(len(mesh_verts))
            mesh_verts.append(tuple(Vector(transformed)))
            if normals:
                try:
                    source_normal = normals[normal_index][:3]
                except (IndexError, TypeError, ValueError):
                    raise ValueError(
                        f"Bone '{bone_name}' poly_mesh normal index {normal_index} is invalid"
                    ) from None
                normal_origin = Vector(transform_cube_vertex(
                    (0.0, 0.0, 0.0), {}, bone_def, bone_name,
                    bones_by_name, bone_matrix_cache,
                ))
                normal_tip = Vector(transform_cube_vertex(
                    source_normal, {}, bone_def, bone_name,
                    bones_by_name, bone_matrix_cache,
                ))
                transformed_normal = normal_tip - normal_origin
                mesh_normals.append(
                    tuple(transformed_normal.normalized())
                    if transformed_normal.length > EPSILON
                    else None
                )
            else:
                mesh_normals.append(None)
            if uvs:
                try:
                    u, v = uvs[uv_index][:2]
                except (IndexError, TypeError, ValueError):
                    raise ValueError(
                        f"Bone '{bone_name}' poly_mesh UV index {uv_index} is invalid"
                    ) from None
                u = float(u)
                v = float(v)
                local_uvs.append(
                    (u, 1.0 - v) if normalized
                    else (u / float(tex_w), 1.0 - (v / float(tex_h)))
                )
            else:
                local_uvs.append((0.0, 0.0))
        mesh_faces.append(tuple(reversed(local_indices)))
        mesh_uvs.append(tuple(reversed(local_uvs)))
    return mesh_verts, mesh_faces, mesh_uvs, mesh_normals

def _uvs_leave_texture(*uv_lists, tolerance=1.0e-5):
    """Whether any face UV lies outside the 0..1 texture square."""
    for uv_list in uv_lists:
        for face_uv in uv_list or ():
            for u, v in face_uv:
                if (
                    u < -tolerance or u > 1.0 + tolerance
                    or v < -tolerance or v > 1.0 + tolerance
                ):
                    return True
    return False


def _collect_texture_mesh(
    texture_meshes,
    texture_path,
    bone_name,
    bone_def,
    bones_by_name,
    bone_matrix_cache,
):
    """Voxelize opaque texels using Bedrock texture-mesh transform semantics."""
    if not isinstance(texture_meshes, list):
        raise ValueError(f"Bone '{bone_name}' texture_meshes must be an array")
    if not texture_path or not os.path.isfile(texture_path):
        raise ValueError(
            f"Bone '{bone_name}' texture_meshes require a resolved source texture"
        )
    try:
        image = bpy.data.images.load(texture_path, check_existing=True)
    except RuntimeError as exc:
        raise ValueError(
            f"Bone '{bone_name}' texture mesh could not load '{texture_path}'"
        ) from exc
    width, height = (int(image.size[0]), int(image.size[1]))
    if width <= 0 or height <= 0:
        raise ValueError(f"Bone '{bone_name}' texture mesh has an empty texture")
    pixels = list(image.pixels[:])
    channels = int(getattr(image, "channels", 4) or 4)

    opaque = set()
    for row in range(height):
        source_row = height - 1 - row
        for column in range(width):
            pixel_index = (source_row * width + column) * channels
            alpha = pixels[pixel_index + 3] if channels >= 4 else 1.0
            if alpha > 0.01:
                opaque.add((column, row))

    mesh_verts = []
    mesh_faces = []
    mesh_uvs = []
    voxel_count = 0
    for texture_mesh in texture_meshes:
        if not isinstance(texture_mesh, dict):
            raise ValueError(f"Bone '{bone_name}' texture mesh entry must be an object")
        position = Vector(coerce_vector(texture_mesh.get("position", [0, 0, 0])))
        local_pivot = Vector(
            coerce_vector(texture_mesh.get("local_pivot", [0, 0, 0]))
        )
        scale = Vector(coerce_vector(texture_mesh.get("scale", [1, 1, 1])))
        rotation = Matrix(
            bedrock_space_rotation_matrix(
                texture_mesh.get("rotation", [0, 0, 0])
            )
        ).to_3x3()

        # Texture geometry spans 16 entity pixels across. The legacy default
        # keeps depth at one entity pixel; false makes every texel a cube.
        pixel_size = 16.0 / float(width)
        depth = 1.0 if texture_mesh.get("use_pixel_depth", True) else pixel_size
        for column, row in sorted(opaque, key=lambda item: (item[1], item[0])):
            x0 = column * pixel_size
            x1 = x0 + pixel_size
            z0 = (height - row - 1) * pixel_size
            z1 = z0 + pixel_size
            y0, y1 = -depth * 0.5, depth * 0.5
            source_vertices = (
                (x0, y0, z0), (x1, y0, z0),
                (x1, y1, z0), (x0, y1, z0),
                (x0, y0, z1), (x1, y0, z1),
                (x1, y1, z1), (x0, y1, z1),
            )
            transformed_vertices = []
            for point in source_vertices:
                local = Vector((
                    (point[0] - local_pivot.x) * scale.x,
                    (point[1] - local_pivot.y) * scale.y,
                    (point[2] - local_pivot.z) * scale.z,
                ))
                model_point = position + (rotation @ local)
                transformed_vertices.append(tuple(Vector(transform_cube_vertex(
                    model_point,
                    {},
                    bone_def,
                    bone_name,
                    bones_by_name,
                    bone_matrix_cache,
                ))))
            base_index = len(mesh_verts)
            mesh_verts.extend(transformed_vertices)
            voxel_count += 1

            exposed = (
                ("down", True),
                ("up", True),
                ("west", (column - 1, row) not in opaque),
                ("east", (column + 1, row) not in opaque),
                ("north", (column, row + 1) not in opaque),
                ("south", (column, row - 1) not in opaque),
            )
            for face_name, visible in exposed:
                if not visible:
                    continue
                face_indices = list(FACE_INDICES[face_name])
                wound_indices = blender_winding(face_indices)
                face_uvs = rect_uv(
                    column, row, 1, 1, width, height, face=face_name
                )
                uv_by_local = dict(zip(face_indices, face_uvs))
                mesh_faces.append(
                    tuple(base_index + index for index in wound_indices)
                )
                mesh_uvs.append(tuple(uv_by_local[index] for index in wound_indices))
    return mesh_verts, mesh_faces, mesh_uvs, voxel_count



def _texture_visibility_lookup(texture_path, alpha=0.5):
    """Return ``face -> has visible texels`` for one texture, or None.

    Texels at alpha 0.5 or more survive Bedrock's alpha test. The UV area is
    read from the face's own corners, so box and per-face UVs, mirroring and
    HD textures (any resolution over the declared size) all agree. A list of
    textures (a mod mob's base and the layers drawn over it) shows a face
    when any of them has a visible texel there.
    """
    if isinstance(texture_path, (list, tuple)):
        lookups = [_texture_visibility_lookup(path, alpha) for path in texture_path]
        if not lookups or any(lookup is None for lookup in lookups):
            return None
        return lambda face: any(lookup(face) for lookup in lookups)
    if not texture_path or not os.path.isfile(texture_path):
        return None
    try:
        import numpy

        image = bpy.data.images.load(texture_path, check_existing=True)
        width, height = int(image.size[0]), int(image.size[1])
        channels = int(getattr(image, "channels", 4) or 4)
        if width <= 0 or height <= 0:
            return None
        if channels < 4:
            return lambda face: True
        pixels = numpy.empty(width * height * channels, dtype=numpy.float32)
        image.pixels.foreach_get(pixels)
    except (RuntimeError, ValueError, ImportError):
        return None
    # Blender stores rows bottom-up, which matches normalized UV V.
    visible = pixels.reshape(height, width, channels)[..., 3] >= alpha

    def face_has_visible_texels(face):
        us = [u for u, _v in face.front_uv.ordered_corners]
        vs = [v for _u, v in face.front_uv.ordered_corners]
        x0 = max(0, int(numpy.floor(min(us) * width + 1e-6)))
        x1 = min(width, int(numpy.ceil(max(us) * width - 1e-6)))
        y0 = max(0, int(numpy.floor(min(vs) * height + 1e-6)))
        y1 = min(height, int(numpy.ceil(max(vs) * height - 1e-6)))
        if x1 <= x0 or y1 <= y0:
            return False
        return bool(visible[y0:y1, x0:x1].any())

    return face_has_visible_texels


def _face_draws(face, face_has_visible_texels):
    """Whether either UV a face carries (plane pairs have two) has a visible texel."""
    if face_has_visible_texels(face):
        return True
    if face.back_uv is None:
        return False
    return bool(face_has_visible_texels(SimpleNamespace(front_uv=face.back_uv)))


def _lift_coplanar_cubes(verts, faces, cube_spans):
    """Move later cubes a hair over earlier coplanar faces, as the game draws them.

    ``cube_spans`` lists each cube's vertices and first face in draw order;
    its faces run until the next cube's first face (poly and texture meshes
    are appended after a bone's cubes, so only faces on the cube's own
    vertices count). See ``bedrock_uv.coplanar_lift_offsets``. Returns the number of
    cubes moved.
    """
    cubes = []
    for position, (first_vertex, count, first_face, is_card, bone) in enumerate(cube_spans):
        last_vertex = first_vertex + count
        last_face = cube_spans[position + 1][2] if position + 1 < len(cube_spans) else len(faces)
        loops = [
            tuple(index - first_vertex for index in face)
            for face in faces[first_face:last_face]
            if all(first_vertex <= index < last_vertex for index in face)
        ]
        cubes.append((verts[first_vertex:last_vertex], loops, is_card, bone))
    lifted = 0
    for (first_vertex, _count, _first_face, _is_card, _bone), moved in zip(cube_spans, coplanar_lift_offsets(cubes)):
        for index, (dx, dy, dz) in moved.items():
            x, y, z = verts[first_vertex + index]
            verts[first_vertex + index] = (x + dx, y + dy, z + dz)
        lifted += bool(moved)
    return lifted


def _collect_geometry_mesh(
    bones_data,
    bones_by_name,
    bone_matrix_cache,
    hidden_bones,
    tex_w,
    tex_h,
    legacy_uv_mode=False,
    skip_cube_signatures=None,
    texture_path="",
    format_version="1.21.0",
    visibility_alpha=0.5,
):
    """Build Blender-space geometry from canonical, source-located face records."""
    skip_cube_signatures = skip_cube_signatures or set()
    verts = []
    faces = []
    loop_uvs = []
    vertex_normals = []
    loop_back_uvs = []
    flat_cap_faces = []
    two_sided_faces = []
    uv_pair_modes = []
    bone_vertex_indices = {
        bone_def.get("name", "bone"): []
        for bone_def in bones_data
        if isinstance(bone_def, dict)
    }
    uv_stats = {
        "faces_generated": 0,
        "faces_skipped_no_uv": 0,
        "zero_thickness_planes": 0,
        "paired_two_sided_planes": 0,
        "cubes_skipped_degenerate": 0,
        "poly_faces_generated": 0,
        "poly_normals_imported": 0,
        "texture_mesh_faces_generated": 0,
        "texture_mesh_voxels": 0,
        "exact_faces": 0,
        "compatibility_faces": 0,
        "visual_only_faces": 0,
        "signed_size_axes_normalized": 0,
        "see_through_faces": 0,
        "issues": [],
        "source_locations": [],
        "face_cube_signatures": [],
    }
    face_has_visible_texels = _texture_visibility_lookup(texture_path, visibility_alpha)
    if isinstance(texture_path, (list, tuple)):  # base texture first
        texture_path = texture_path[0] if texture_path else ""
    # (first vertex, vertex count, first face, collapsed card, bone) per cube, in draw order.
    cube_spans = []

    for bone_index, bone_def in enumerate(bones_data):
        if not isinstance(bone_def, dict):
            continue
        bone_name = bone_def.get("name", "bone")
        if bool(bone_def.get("neverRender", bone_def.get("never_render", False))):
            continue
        if _is_bone_hidden(bone_name, bones_by_name, hidden_bones):
            continue
        effective_bone_def = bones_by_name.get(bone_name, bone_def)
        for cube_index, cube in enumerate(bone_def.get("cubes", [])):
            if not isinstance(cube, dict):
                continue
            if cube_signature(bone_name, cube) in skip_cube_signatures:
                continue
            source_cube_key = cube_signature_key(bone_name, cube, bones_by_name)
            origin = coerce_vector(cube.get("origin", [0, 0, 0]))
            size = coerce_vector(cube.get("size", [0, 0, 0]))
            topology_cube, origin, size = normalize_signed_cube_geometry(
                cube, origin, size
            )
            uv_stats["signed_size_axes_normalized"] += len(
                topology_cube.get("_mcbedrock_signed_size_axes", ())
            )
            inflate = coerce_float(
                cube.get("inflate", bone_def.get("inflate", 0.0))
            )
            ox, oy, oz = origin
            sx, sy, sz = size
            uv_sx, uv_sy, uv_sz = sx, sy, sz
            if cube.get("_mcbedrock_uv_size") is not None:
                # Mod entity parts scaled in code: the drawn box is baked,
                # the box-UV layout keeps the authored sizes.
                uv_sx, uv_sy, uv_sz = coerce_vector(cube["_mcbedrock_uv_size"])
            ox -= inflate
            oy -= inflate
            oz -= inflate
            sx += inflate * 2.0
            sy += inflate * 2.0
            sz += inflate * 2.0

            cube_path = (
                f"$.bones[{bone_index}].cubes[{cube_index}]"
            )
            cube_result = build_cube_faces(
                topology_cube,
                ox,
                oy,
                oz,
                sx,
                sy,
                sz,
                tex_w,
                tex_h,
                mirror=bool(topology_cube.get("mirror", bone_def.get("mirror", False))),
                legacy_uv_mode=legacy_uv_mode,
                uv_sx=uv_sx,
                uv_sy=uv_sy,
                uv_sz=uv_sz,
                format_version=format_version,
                source_location=cube_path,
            )
            uv_stats["issues"].extend(
                issue.as_dict() for issue in cube_result.issues
            )
            if (
                face_has_visible_texels is not None
                and cube.get("_mcbedrock_java_entity_cube")
            ):
                # Java entity cutout discards transparent texels, so a face
                # without one never draws. Kept, it would z-fight with the
                # face of a cube it touches (a mob's spine ridge on its back).
                drawn = [
                    face for face in cube_result.faces
                    if _face_draws(face, face_has_visible_texels)
                ]
                uv_stats["faces_skipped_invisible"] = (
                    uv_stats.get("faces_skipped_invisible", 0)
                    + len(cube_result.faces) - len(drawn)
                )
                cube_result.faces = drawn
            if not cube_result.vertices or not cube_result.faces:
                uv_stats["cubes_skipped_degenerate"] += 1
                continue
            if face_has_visible_texels is not None:
                uv_stats["see_through_faces"] += mark_see_through_faces(
                    cube_result, face_has_visible_texels
                )
            uv_stats["zero_thickness_planes"] += (
                cube_result.zero_thickness_planes
            )
            uv_stats["paired_two_sided_planes"] += (
                cube_result.paired_two_sided_planes
            )
            uv_stats["faces_skipped_no_uv"] += (
                cube_result.faces_skipped_no_uv
            )

            cube_verts = [
                Vector(transform_cube_vertex(v, topology_cube, effective_bone_def, bone_name, bones_by_name, bone_matrix_cache))
                for v in cube_result.vertices
            ]

            cube_vert_tuples = [tuple(v) for v in cube_verts]
            base_index = len(verts)
            cube_spans.append((base_index, len(cube_verts), len(faces), bool(cube_result.zero_thickness_planes), bone_name))
            verts.extend(cube_vert_tuples)
            vertex_normals.extend([None] * len(cube_vert_tuples))
            bone_vertex_indices.setdefault(bone_name, []).extend(
                range(base_index, base_index + len(cube_verts))
            )
            for emitted in cube_result.faces:
                face_indices = list(emitted.vertex_indices)
                absolute_indices = [
                    base_index + index for index in face_indices
                ]
                # bedrock_to_blender mirrors an axis, so reverse the loop to
                # keep face normals pointing outward in Blender space.
                wound_indices = blender_winding(face_indices)
                uv_by_local = dict(zip(
                    face_indices, emitted.front_uv.ordered_corners
                ))
                back_corners = (
                    emitted.back_uv.ordered_corners
                    if emitted.back_uv is not None
                    else emitted.front_uv.ordered_corners
                )
                back_uv_by_local = dict(zip(face_indices, back_corners))
                absolute_by_local = dict(zip(
                    face_indices, absolute_indices
                ))
                wound_uvs = [uv_by_local[index] for index in wound_indices]
                wound_back_uvs = [
                    back_uv_by_local[index] for index in wound_indices
                ]
                if (
                    len(wound_uvs) != 4
                    or not uv_corners_are_valid(wound_uvs)
                    or not uv_corners_are_valid(wound_back_uvs)
                ):
                    raise ValueError(
                        "Invalid canonical cube UV loop at "
                        f"{emitted.source_location}"
                    )
                is_thin_plane = bool(cube_result.zero_thickness_planes)
                # Collapsed planes and faces seen through their own cube stay
                # two-sided; every other closed face is culled.
                is_two_sided = bool(emitted.two_sided)

                faces.append(tuple(
                    absolute_by_local[index] for index in wound_indices
                ))
                loop_uvs.append(wound_uvs)
                loop_back_uvs.append(wound_back_uvs)
                flat_cap_faces.append(is_thin_plane)
                two_sided_faces.append(is_two_sided)
                uv_pair_modes.append(emitted.pair_mode)
                uv_stats["faces_generated"] += 1
                validity_key = f"{emitted.validity.value.lower()}_faces"
                uv_stats[validity_key] += 1
                uv_stats["source_locations"].append({
                    "front": emitted.source_location,
                    "back": emitted.back_source_location,
                    "direction": emitted.direction,
                    "back_direction": emitted.back_direction,
                    "pair_mode": emitted.pair_mode,
                    "two_sided": is_two_sided,
                    "two_sided_reason": emitted.two_sided_reason,
                    "physical_side": "shader",
                })
                uv_stats["face_cube_signatures"].append(source_cube_key)
        poly_mesh = bone_def.get("poly_mesh")
        if poly_mesh is not None:
            poly_verts, poly_faces, poly_uvs, poly_normals = _collect_poly_mesh(
                poly_mesh,
                bone_name,
                effective_bone_def,
                bones_by_name,
                bone_matrix_cache,
                tex_w,
                tex_h,
            )
            base_index = len(verts)
            verts.extend(poly_verts)
            vertex_normals.extend(poly_normals)
            faces.extend(
                tuple(base_index + index for index in polygon)
                for polygon in poly_faces
            )
            loop_uvs.extend(poly_uvs)
            loop_back_uvs.extend(poly_uvs)
            flat_cap_faces.extend([False] * len(poly_faces))
            two_sided_faces.extend([False] * len(poly_faces))
            uv_pair_modes.extend(["none"] * len(poly_faces))
            bone_vertex_indices.setdefault(bone_name, []).extend(
                range(base_index, base_index + len(poly_verts))
            )
            uv_stats["faces_generated"] += len(poly_faces)
            uv_stats["poly_faces_generated"] += len(poly_faces)
            uv_stats["poly_normals_imported"] += sum(
                normal is not None for normal in poly_normals
            )
        texture_meshes = bone_def.get("texture_meshes")
        if isinstance(texture_meshes, list) and not (
            texture_path and os.path.isfile(texture_path)
        ):
            # A held item's texture comes from the item at runtime. Without
            # one there is nothing to voxelize; the rest of the model imports.
            uv_stats["issues"].append(texture_mesh_skipped_issue(
                bone_name,
                source_location=f"$.bones[{bone_index}].texture_meshes",
            ).as_dict())
            texture_meshes = None
        if texture_meshes is not None:
            texture_verts, texture_faces, texture_uvs, voxel_count = (
                _collect_texture_mesh(
                    texture_meshes,
                    texture_path,
                    bone_name,
                    effective_bone_def,
                    bones_by_name,
                    bone_matrix_cache,
                )
            )
            base_index = len(verts)
            verts.extend(texture_verts)
            vertex_normals.extend([None] * len(texture_verts))
            faces.extend(
                tuple(base_index + index for index in polygon)
                for polygon in texture_faces
            )
            loop_uvs.extend(texture_uvs)
            loop_back_uvs.extend(texture_uvs)
            flat_cap_faces.extend([False] * len(texture_faces))
            two_sided_faces.extend([False] * len(texture_faces))
            uv_pair_modes.extend(["none"] * len(texture_faces))
            bone_vertex_indices.setdefault(bone_name, []).extend(
                range(base_index, base_index + len(texture_verts))
            )
            uv_stats["faces_generated"] += len(texture_faces)
            uv_stats["texture_mesh_faces_generated"] += len(texture_faces)
            uv_stats["texture_mesh_voxels"] += voxel_count

        # Poly/texture meshes have no cube identity and therefore cannot be
        # coalesced with exact cuboid overlays from another controller pass.
        non_cube_faces = (
            len(poly_faces) if poly_mesh is not None else 0
        ) + (
            len(texture_faces) if texture_meshes is not None else 0
        )
        if non_cube_faces:
            uv_stats["face_cube_signatures"].extend([""] * non_cube_faces)

    uv_stats["cubes_lifted_coplanar"] = _lift_coplanar_cubes(verts, faces, cube_spans)
    return (
        verts, faces, loop_uvs, loop_back_uvs, vertex_normals,
        bone_vertex_indices, flat_cap_faces, two_sided_faces, uv_pair_modes, uv_stats,
    )


def _create_skinned_mesh_object(context, group_col, arm_obj, object_name, identifier, verts, faces, loop_uvs, bone_vertex_indices, texture_path, interpolation, render_method, tex_w, tex_h, uv_stats, texture_paths=None, loop_back_uvs=None, vertex_normals=None, flat_cap_faces=None, two_sided_faces=None, uv_pair_modes=None, solid_backface_culling=True):
    """Create a mesh object bound to ``arm_obj`` via an Armature modifier."""
    requested_texture_paths = list(texture_paths or [])
    if texture_path and texture_path not in requested_texture_paths:
        requested_texture_paths.insert(0, texture_path)
    mesh_data = bpy.data.meshes.new(f"{object_name}_MeshData")
    mesh_data.from_pydata(verts, [], faces)
    try:
        mesh_data.validate(clean_customdata=False)
    except TypeError:
        mesh_data.validate()
    mesh_data.update(calc_edges=True)
    uv_layer = mesh_data.uv_layers.new(name="UVMap")
    loop_back_uvs = loop_uvs if loop_back_uvs is None else loop_back_uvs
    if (
        len(loop_uvs) != len(mesh_data.polygons)
        or len(loop_back_uvs) != len(mesh_data.polygons)
    ):
        raise ValueError(
            f"UV/face count mismatch for geometry '{identifier}': "
            f"{len(loop_uvs)} UV entries vs {len(mesh_data.polygons)} polygons"
        )
    has_distinct_back_uvs = any(
        tuple(front) != tuple(back)
        for front, back in zip(loop_uvs, loop_back_uvs)
    )
    back_uv_layer = (
        mesh_data.uv_layers.new(name="UVMap_Back")
        if has_distinct_back_uvs
        else None
    )
    for poly_index, polygon in enumerate(mesh_data.polygons):
        face_uv = loop_uvs[poly_index]
        back_face_uv = loop_back_uvs[poly_index]
        for i, loop_index in enumerate(polygon.loop_indices):
            uv_layer.data[loop_index].uv = face_uv[i]
            if back_uv_layer is not None:
                back_uv_layer.data[loop_index].uv = back_face_uv[i]

    flat_cap_faces = list(flat_cap_faces or ())
    if flat_cap_faces:
        if len(flat_cap_faces) != len(mesh_data.polygons):
            raise ValueError(
                f"Flat-cap/face count mismatch for geometry '{identifier}'"
            )
        attribute = mesh_data.attributes.new(
            "mcbedrock_flat_cap", type="BOOLEAN", domain="FACE"
        )
        for index, is_flat_cap in enumerate(flat_cap_faces):
            attribute.data[index].value = bool(is_flat_cap)

    two_sided_faces = list(two_sided_faces or ())
    if not two_sided_faces:
        two_sided_faces = list(flat_cap_faces)
    if len(two_sided_faces) != len(mesh_data.polygons):
        raise ValueError(
            f"Two-sided/face count mismatch for geometry '{identifier}'"
        )
    two_sided_attribute = mesh_data.attributes.new(
        "mcbedrock_two_sided_face", type="BOOLEAN", domain="FACE"
    )
    for index, is_two_sided in enumerate(two_sided_faces):
        two_sided_attribute.data[index].value = bool(is_two_sided)

    uv_pair_modes = list(uv_pair_modes or ("none" for _ in mesh_data.polygons))
    if len(uv_pair_modes) != len(mesh_data.polygons):
        raise ValueError(
            f"UV-pair/face count mismatch for geometry '{identifier}'"
        )
    pair_mode_attribute = mesh_data.attributes.new(
        "mcbedrock_uv_pair_mode", type="INT", domain="FACE"
    )
    pair_codes = {"none": 0, "directional": 1, "composite": 2}
    for index, pair_mode in enumerate(uv_pair_modes):
        pair_mode_attribute.data[index].value = pair_codes.get(pair_mode, 0)

    mesh_obj = bpy.data.objects.new(f"{object_name}_Mesh", mesh_data)

    if vertex_normals and any(normal is not None for normal in vertex_normals):
        if len(vertex_normals) != len(mesh_data.vertices):
            raise ValueError(
                f"Normal/vertex count mismatch for geometry '{identifier}': "
                f"{len(vertex_normals)} normals vs {len(mesh_data.vertices)} vertices"
            )
        loop_normals = []
        for polygon in mesh_data.polygons:
            fallback = tuple(polygon.normal)
            for loop_index in polygon.loop_indices:
                vertex_index = mesh_data.loops[loop_index].vertex_index
                normal = vertex_normals[vertex_index]
                loop_normals.append(tuple(normal) if normal is not None else fallback)
        mesh_data.normals_split_custom_set(loop_normals)
        mesh_data.update()
    context.collection.objects.link(mesh_obj)
    group_col.objects.link(mesh_obj)
    for col in list(mesh_obj.users_collection):
        if col != group_col:
            col.objects.unlink(mesh_obj)
    mesh_obj.parent = arm_obj
    mesh_obj.parent_type = "OBJECT"
    mesh_obj.location = (0.0, 0.0, 0.0)
    mesh_obj.rotation_euler = (0.0, 0.0, 0.0)
    mesh_obj.scale = (1.0, 1.0, 1.0)
    for lock_attr in ("lock_location", "lock_rotation", "lock_scale"):
        setattr(mesh_obj, lock_attr, (True, True, True))
    modifier = mesh_obj.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = arm_obj

    for bone_name, indices in bone_vertex_indices.items():
        if not indices:
            continue
        group = mesh_obj.vertex_groups.new(name=bone_name)
        group.add(indices, 1.0, "ADD")

    culled_slot = None
    if any(not value for value in two_sided_faces):
        culled_slot = len(mesh_obj.data.materials)
        mesh_obj.data.materials.append(make_model_material(
            object_name,
            texture_path,
            interpolation=interpolation,
            render_method=render_method,
            texture_paths=requested_texture_paths,
            two_sided_uv=has_distinct_back_uvs,
            backface_culling=solid_backface_culling,
        ))
    two_sided_slot = None
    if any(two_sided_faces):
        two_sided_slot = len(mesh_obj.data.materials)
        mesh_obj.data.materials.append(make_model_material(
            f"{object_name}_TwoSided",
            texture_path,
            interpolation=interpolation,
            render_method=render_method,
            texture_paths=requested_texture_paths,
            two_sided_uv=has_distinct_back_uvs,
            force_two_sided=True,
        ))
    if not mesh_obj.data.materials:
        culled_slot = 0
        mesh_obj.data.materials.append(make_model_material(
            object_name,
            texture_path,
            interpolation=interpolation,
            render_method=render_method,
            texture_paths=requested_texture_paths,
            two_sided_uv=has_distinct_back_uvs,
            backface_culling=solid_backface_culling,
        ))
    for polygon, is_two_sided in zip(
        mesh_obj.data.polygons, two_sided_faces
    ):
        polygon.material_index = (
            two_sided_slot
            if is_two_sided and two_sided_slot is not None
            else culled_slot
        )
    if _uvs_leave_texture(loop_uvs, loop_back_uvs):
        # Entity textures clamp in game: past the edge, the edge pixels
        # repeat. CLIP would leave holes there instead. Inside the texture
        # both sample the same texels, so only these models switch.
        for material in mesh_obj.data.materials:
            for node in material.node_tree.nodes if material and material.node_tree else ():
                if node.bl_idname == "ShaderNodeTexImage" and node.extension == "CLIP":
                    node.extension = "EXTEND"
        mesh_obj["mcbedrock_uv_outside_texture"] = True
    mesh_obj["mcbedrock_asset_type"] = "model_mesh"
    mesh_obj["mcbedrock_geometry_id"] = identifier
    mesh_obj["mcbedrock_texture_size"] = f"{tex_w}x{tex_h}"
    mesh_obj["mcbedrock_uv_faces"] = uv_stats["faces_generated"]
    mesh_obj["mcbedrock_uv_skipped"] = uv_stats["faces_skipped_no_uv"]
    mesh_obj["mcbedrock_uv_zero_planes"] = uv_stats["zero_thickness_planes"]
    mesh_obj["mcbedrock_uv_paired_planes"] = uv_stats.get("paired_two_sided_planes", 0)
    mesh_obj["mcbedrock_cubes_degenerate"] = uv_stats["cubes_skipped_degenerate"]
    mesh_obj["mcbedrock_texture_mesh_faces"] = uv_stats.get("texture_mesh_faces_generated", 0)
    mesh_obj["mcbedrock_texture_mesh_voxels"] = uv_stats.get("texture_mesh_voxels", 0)
    mesh_obj["mcbedrock_uv_exact_faces"] = uv_stats.get("exact_faces", 0)
    mesh_obj["mcbedrock_uv_compatibility_faces"] = uv_stats.get(
        "compatibility_faces", 0
    )
    mesh_obj["mcbedrock_uv_visual_only_faces"] = uv_stats.get(
        "visual_only_faces", 0
    )
    mesh_obj["mcbedrock_uv_issues"] = json.dumps(uv_stats.get("issues", []))
    mesh_obj["mcbedrock_face_sources"] = json.dumps(uv_stats.get("source_locations", []))
    mesh_obj["mcbedrock_face_cube_signatures"] = json.dumps(
        uv_stats.get("face_cube_signatures", [])
    )
    mesh_obj["mcbedrock_poly_faces"] = uv_stats.get("poly_faces_generated", 0)
    mesh_obj["mcbedrock_poly_normals"] = uv_stats.get("poly_normals_imported", 0)
    return mesh_obj


def add_bedrock_model_layer(context, arm_obj, group_col, geometry, texture_path="", interpolation="Closest", origin_offset=None, hidden_bones=None, object_suffix="", render_method="alpha_test", skip_cube_signatures=None, canonical_bones=None, texture_paths=None, solid_backface_culling=True):
    """Attach an extra geometry (attachable/prop layer) to an existing armature.

    Missing bones from the layer geometry are added to the armature so a
    shared animation can drive them, then the layer's cubes are built as a
    separate mesh object skinned to that armature.
    """
    hidden_bones = {str(name) for name in (hidden_bones or [])}
    if not isinstance(geometry, dict):
        raise ValueError("Bedrock geometry entry is not an object")

    description = geometry.get("description", {}) if isinstance(geometry.get("description"), dict) else {}
    identifier = description.get("identifier", "unknown_layer")
    tex_w = float(description.get("texture_width", description.get("texturewidth", 64)) or 64)
    tex_h = float(description.get("texture_height", description.get("textureheight", 64)) or 64)
    object_name = _clean_import_name(f"{arm_obj.name} {object_suffix}".strip(), arm_obj.name)

    bones_data = geometry.get("bones", [])
    bones_data = bones_data if isinstance(bones_data, list) else []
    original_bone_count = len(bones_data)
    bones_data = _merge_duplicate_bones(bones_data)
    control_length = _armature_control_length(bones_data)
    layer_bones_by_name = {bone.get("name", "bone"): bone for bone in bones_data if isinstance(bone, dict)}
    # Build each layer in its own authored skeleton space. Shared bone names
    # bind to the existing armature at rest, while using the base geometry's
    # pivots here would move the layer before binding even begins.
    bones_by_name = layer_bones_by_name
    bone_matrix_cache = {}


    layer_names = set(layer_bones_by_name)
    missing = [
        bones_by_name[name]
        for name in topological_bone_names(list(bones_by_name.values()))
        if name in layer_names and name not in arm_obj.data.bones
    ]
    if missing:
        previous_active = context.view_layer.objects.active
        deselect_all_objects(context)
        arm_obj.select_set(True)
        context.view_layer.objects.active = arm_obj
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = arm_obj.data.edit_bones
        rest_cache = {}
        for bone_def in missing:
            bone_name = bone_def.get("name", "bone")
            if bone_name in edit_bones:
                continue

            authored_parent = bone_def.get("parent")
            parent_name = _resolve_literal_parent(
                authored_parent, edit_bones.keys(), bone_name
            )
            binding = bone_def.get("binding")
            binding_target = _resolve_binding_parent(binding, edit_bones.keys())
            if not parent_name and not authored_parent and binding_target:
                parent_name = binding_target
            if parent_name != authored_parent:
                bone_def = dict(bone_def)
                if parent_name:
                    bone_def["parent"] = parent_name
                else:
                    bone_def.pop("parent", None)
                bones_by_name[bone_name] = bone_def
            if not parent_name and "entity_root" in edit_bones:
                parent_name = "entity_root"

            if parent_name and parent_name in edit_bones and parent_name not in bones_by_name:
                bone_matrix_cache[parent_name] = Matrix.Translation(edit_bones[parent_name].head)

            edit_bone = edit_bones.new(bone_name)
            edit_bone.head = Vector((0.0, 0.0, 0.0))
            edit_bone.tail = Vector((0.0, control_length, 0.0))
            edit_bone["mcbedrock_rest_rotation"] = [float(v) for v in bone_rotation(bone_def)]
        if bone_def.get("_mcbedrock_native_reflected"):
            edit_bone["mcbedrock_native_reflected"] = str(
                bone_def["_mcbedrock_native_reflected"]
            )
            if bone_def.get("_mcbedrock_native_reflected"):
                edit_bone["mcbedrock_native_reflected"] = str(
                    bone_def["_mcbedrock_native_reflected"]
                )
            edit_bone["mcbedrock_pivot"] = [float(v) for v in bedrock_to_blender(bone_def.get("pivot", [0.0, 0.0, 0.0]))]
            edit_bone["mcbedrock_parent"] = str(parent_name or "")
            if isinstance(binding, str) and binding:
                edit_bone["mcbedrock_binding"] = binding
                edit_bone["mcbedrock_binding_target"] = str(binding_target or "")
            if parent_name and parent_name in edit_bones:
                edit_bone.parent = edit_bones[parent_name]
                edit_bone.use_connect = False
            source_rest = _bone_rest_world_matrix(
                bone_name, bones_by_name, rest_cache
            )
            edit_bone.matrix = source_rest
            edit_bone["mcbedrock_source_rest_matrix"] = [
                float(source_rest[row][column])
                for row in range(4)
                for column in range(4)
            ]
            bind_matrix = _bone_bind_matrix(bone_name, bones_by_name, bone_matrix_cache)
            edit_bone["mcbedrock_bind_matrix"] = [
                float(bind_matrix[row][column])
                for row in range(4)
                for column in range(4)
            ]
            if (edit_bone.head - edit_bone.tail).length < 0.001:
                edit_bone.tail = edit_bone.head + Vector((0, control_length, 0))
        _fit_edit_bone_tails(
            edit_bones,
            bones_data,
            bones_by_name,
            bone_matrix_cache,
            control_length,
            target_names={
                bone.get("name", "bone")
                for bone in missing if isinstance(bone, dict)
            },
        )
        bpy.ops.object.mode_set(mode="OBJECT")
        if previous_active and previous_active.name in bpy.data.objects:
            context.view_layer.objects.active = previous_active

    (
        verts, faces, loop_uvs, loop_back_uvs, vertex_normals,
        bone_vertex_indices, flat_cap_faces, two_sided_faces, uv_pair_modes, uv_stats,
    ) = _collect_geometry_mesh(
        bones_data, bones_by_name, bone_matrix_cache, hidden_bones, tex_w, tex_h,
        legacy_uv_mode=bool(description.get("legacy_uv", False)),
        skip_cube_signatures=skip_cube_signatures,
        # Mod mobs list every texture drawn over the base: a face shows
        # where any of them has a visible texel.
        texture_path=geometry.get("_mcbedrock_visibility_textures")
        or texture_path or next(iter(texture_paths or ()), ""),
        format_version=str(geometry.get("_mcbedrock_format_version", "1.21.0")),
        # Java Edition's entity cutout keeps texels from alpha 0.1, Bedrock's from 0.5.
        visibility_alpha=float(geometry.get("_mcbedrock_visibility_alpha", 0.5)),
    )
    if not verts or not faces:
        raise ValueError(f"Geometry layer '{identifier}' has no renderable mesh")


    mesh_obj = _create_skinned_mesh_object(
        context, group_col, arm_obj, object_name, identifier,
        verts, faces, loop_uvs, bone_vertex_indices,
        texture_path, interpolation, render_method, tex_w, tex_h, uv_stats,
        texture_paths=texture_paths,
        loop_back_uvs=loop_back_uvs,
        vertex_normals=vertex_normals,
        flat_cap_faces=flat_cap_faces,
        two_sided_faces=two_sided_faces,
        solid_backface_culling=solid_backface_culling,
        uv_pair_modes=uv_pair_modes,
    )
    mesh_obj["mcbedrock_format_version"] = str(
        geometry.get("_mcbedrock_format_version", "")
    )
    mesh_obj["mcbedrock_duplicate_bones_collapsed"] = original_bone_count - len(bones_data)
    mesh_obj["mcbedrock_schema_family"] = str(
        geometry.get("_mcbedrock_schema_family", "")
    )
    mesh_obj["mcbedrock_layer"] = object_suffix or identifier
    return mesh_obj


def build_bedrock_model(context, geometry, name_prefix="", texture_path="", source_path="", interpolation="Closest", display_name="", origin_offset=None, collection=None, hidden_bones=None, object_suffix="", render_method="alpha_test", texture_paths=None, solid_backface_culling=True, presentation_rotation=None):
    hidden_bones = {str(name) for name in (hidden_bones or [])}
    owns_collection = collection is None
    if not isinstance(geometry, dict):
        raise ValueError("Bedrock geometry entry is not an object")
    geometry = deepcopy(geometry)
    format_version = str(geometry.get("_mcbedrock_format_version", ""))
    schema_family = str(geometry.get("_mcbedrock_schema_family", ""))
    case_names = {
        str(bone.get("name", "")).lower(): str(bone.get("name", ""))
        for bone in geometry.get("bones", [])
        if isinstance(bone, dict) and bone.get("name")
    }
    for bone in geometry.get("bones", []):
        if not isinstance(bone, dict):
            continue
        parent = bone.get("parent")
        if isinstance(parent, str) and parent.lower() in case_names:
            bone["parent"] = case_names[parent.lower()]

    description = geometry.get("description", {}) if isinstance(geometry.get("description"), dict) else {}
    identifier = description.get("identifier", "unknown_model")
    fallback_name = safe_name(f"{name_prefix}{identifier.split(':')[-1]}")
    if source_path and (len(identifier) < 4 or any(part.isdigit() and len(part) >= 6 for part in identifier.replace(".", "_").split("_"))):
        fallback_name = safe_name(os.path.splitext(os.path.basename(source_path))[0])
    armature_name = _clean_import_name(display_name, fallback_name)
    object_name = _clean_import_name(f"{armature_name} {object_suffix}".strip(), armature_name)
    tex_w = float(description.get("texture_width", description.get("texturewidth", 64)) or 64)
    tex_h = float(description.get("texture_height", description.get("textureheight", 64)) or 64)

    group_col = collection or ensure_unique_collection(context, f"MC_Model_{armature_name}")
    arm_data = bpy.data.armatures.new(f"{object_name}_Armature")
    arm_obj = bpy.data.objects.new(object_name, arm_data)
    _set_armature_display(arm_data, arm_obj)
    context.collection.objects.link(arm_obj)
    group_col.objects.link(arm_obj)
    for col in list(arm_obj.users_collection):
        if col != group_col:
            col.objects.unlink(arm_obj)

    deselect_all_objects(context)
    arm_obj.select_set(True)
    context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")

    bones_data = geometry.get("bones", [])
    bones_data = bones_data if isinstance(bones_data, list) else []
    original_bone_count = len(bones_data)
    bones_data = _merge_duplicate_bones(bones_data)
    control_length = _armature_control_length(bones_data)
    bones_by_name = {bone.get("name", "bone"): bone for bone in bones_data if isinstance(bone, dict)}
    bone_matrix_cache = {}
    bone_objs = {}
    for bone_def in bones_data:
        if not isinstance(bone_def, dict):
            continue
        bone_name = bone_def.get("name", "bone")
        edit_bone = arm_data.edit_bones.new(bone_name)
        edit_bone.head = Vector((0.0, 0.0, 0.0))
        edit_bone.tail = Vector((0.0, control_length, 0.0))
        edit_bone["mcbedrock_rest_rotation"] = [float(v) for v in bone_rotation(bone_def)]
        if bone_def.get("_mcbedrock_native_reflected"):
            edit_bone["mcbedrock_native_reflected"] = str(
                bone_def["_mcbedrock_native_reflected"]
            )
        edit_bone["mcbedrock_pivot"] = [float(v) for v in bedrock_to_blender(bone_def.get("pivot", [0.0, 0.0, 0.0]))]

        authored_parent = bone_def.get("parent")
        parent_name = _resolve_literal_parent(
            authored_parent, bones_by_name.keys(), bone_name
        )
        binding = bone_def.get("binding")
        binding_target = _resolve_binding_parent(binding, bones_by_name.keys())
        if not parent_name and not authored_parent and binding_target:
            parent_name = binding_target
        if parent_name != authored_parent:
            effective_def = dict(bone_def)
            if parent_name:
                effective_def["parent"] = parent_name
            else:
                effective_def.pop("parent", None)
            bones_by_name[bone_name] = effective_def
        edit_bone["mcbedrock_parent"] = str(parent_name or "")
        if isinstance(binding, str) and binding:
            edit_bone["mcbedrock_binding"] = binding
            edit_bone["mcbedrock_binding_target"] = str(binding_target or "")

        bone_objs[bone_name] = edit_bone

    for bone_def in bones_data:
        if not isinstance(bone_def, dict):
            continue
        bone_name = bone_def.get("name", "bone")
        parent_name = bone_objs[bone_name].get("mcbedrock_parent", "") if bone_name in bone_objs else ""
        if parent_name and parent_name in bone_objs and bone_name in bone_objs:
            bone_objs[bone_name].parent = bone_objs[parent_name]
            bone_objs[bone_name].use_connect = False

    root_bones = [bone for bone in bone_objs.values() if bone.parent is None]
    if len(root_bones) > 1:
        synthetic_root = arm_data.edit_bones.new("entity_root")
        synthetic_root.head = Vector((0.0, 0.0, 0.0))
        synthetic_root.tail = Vector((0.0, control_length, 0.0))
        synthetic_root["mcbedrock_synthetic_root"] = True
        for root_bone in root_bones:
            root_bone.parent = synthetic_root
            root_bone.use_connect = False

    rest_cache = {}
    for bone_name in topological_bone_names(list(bones_by_name.values())):
        if bone_name in bone_objs:
            source_rest = _bone_rest_world_matrix(
                bone_name, bones_by_name, rest_cache
            )
            bone_objs[bone_name].matrix = source_rest
            bone_objs[bone_name]["mcbedrock_source_rest_matrix"] = [
                float(source_rest[row][column])
                for row in range(4)
                for column in range(4)
            ]
            bind_matrix = _bone_bind_matrix(bone_name, bones_by_name, bone_matrix_cache)
            bone_objs[bone_name]["mcbedrock_bind_matrix"] = [
                float(bind_matrix[row][column])
                for row in range(4)
                for column in range(4)
            ]

    _fit_edit_bone_tails(
        arm_data.edit_bones, bones_data, bones_by_name,
        bone_matrix_cache, control_length,
    )
    bpy.ops.object.mode_set(mode="OBJECT")

    (
        verts, faces, loop_uvs, loop_back_uvs, vertex_normals,
        bone_vertex_indices, flat_cap_faces, two_sided_faces, uv_pair_modes, uv_stats,
    ) = _collect_geometry_mesh(
        bones_data, bones_by_name, bone_matrix_cache, hidden_bones, tex_w, tex_h,
        legacy_uv_mode=bool(description.get("legacy_uv", False)),
        # Mod mobs list every texture drawn over the base: a face shows
        # where any of them has a visible texel.
        texture_path=geometry.get("_mcbedrock_visibility_textures")
        or texture_path or next(iter(texture_paths or ()), ""),
        format_version=str(geometry.get("_mcbedrock_format_version", "1.21.0")),
        # Java Edition's entity cutout keeps texels from alpha 0.1, Bedrock's from 0.5.
        visibility_alpha=float(geometry.get("_mcbedrock_visibility_alpha", 0.5)),
    )

    if not verts or not faces:
        bpy.data.objects.remove(arm_obj, do_unlink=True)
        if owns_collection and group_col and not group_col.objects:
            bpy.data.collections.remove(group_col)
        raise ValueError(f"Geometry '{identifier}' has no renderable mesh. Choose another geometry entry from the asset browser.")

    if origin_offset is None:
        origin_offset = _origin_offset_from_verts(verts)
    else:
        origin_offset = Vector(origin_offset)

    mesh_obj = _create_skinned_mesh_object(
        context, group_col, arm_obj, object_name, identifier,
        verts, faces, loop_uvs, bone_vertex_indices,
        texture_path, interpolation, render_method, tex_w, tex_h, uv_stats,
        texture_paths=texture_paths,
        loop_back_uvs=loop_back_uvs,
        vertex_normals=vertex_normals,
        flat_cap_faces=flat_cap_faces,
        two_sided_faces=two_sided_faces,
        solid_backface_culling=solid_backface_culling,
        uv_pair_modes=uv_pair_modes,
    )
    arm_obj["mcbedrock_asset_type"] = "model"
    arm_obj["mcbedrock_identifier"] = identifier
    arm_obj["mcbedrock_source_path"] = source_path
    arm_obj["mcbedrock_texture"] = texture_path
    arm_obj["mcbedrock_origin_offset"] = tuple(origin_offset)
    arm_obj["mcbedrock_renderer_family"] = str(
        description.get("mcbedrock_renderer_family",
            description.get("_mcbedrock_renderer_family", "authored_geometry"))
    )
    arm_obj["mcbedrock_format_version"] = format_version
    arm_obj["mcbedrock_duplicate_bones_collapsed"] = original_bone_count - len(bones_data)
    arm_obj["mcbedrock_schema_family"] = schema_family
    # Export must use the same resolved geometry and visibility state that
    # created the Blender mesh. Re-reading raw geometry can resurrect mutually
    # exclusive renderer parts as duplicate, static Blockbench elements.
    try:
        arm_obj["mcbedrock_export_geometry"] = json.dumps(
            geometry, separators=(",", ":")
        )
    except (TypeError, ValueError):
        # Raw-source fallback remains available for unusual non-JSON payloads.
        pass
    arm_obj["mcbedrock_export_hidden_parts"] = json.dumps(
        sorted(hidden_bones)
    )
    mesh_obj["mcbedrock_format_version"] = format_version
    mesh_obj["mcbedrock_schema_family"] = schema_family
    if display_name:
        arm_obj["mcbedrock_display_name"] = display_name

    if presentation_rotation is not None:
        arm_obj.rotation_mode = "XYZ"
        arm_obj.rotation_euler = tuple(float(value) for value in presentation_rotation)
        arm_obj["mcbedrock_presentation_rotation"] = tuple(
            float(value) for value in presentation_rotation
        )
        context.view_layer.update()

    finalize_bedrock_rig(context, arm_obj)
    if owns_collection:
        recenter_bedrock_composite(context, arm_obj, policy="GROUNDED")
        place_transform_origin_below_model(context, arm_obj)
    return {"armature": arm_obj, "mesh": mesh_obj, "collection": group_col, "origin_offset": origin_offset}
