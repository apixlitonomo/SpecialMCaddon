"""Presentation-origin helpers for Bedrock model imports."""

import bpy
from mathutils import Matrix, Vector
from .selection import deselect_all_objects


def create_presentation_root(context, active_object, mesh_objects=None):
    """Create a transform handle without modifying source object data.

    The wrapper is placed below the evaluated visible bounds. Parenting keeps
    the armature's world matrix unchanged, so source vertices, rest matrices,
    pivots, and animation channels are byte-for-byte unaffected.
    """
    if active_object is None or active_object.type not in {"ARMATURE", "MESH"}:
        return None
    existing = active_object.parent
    if existing is not None and existing.get("mcbedrock_presentation_root", False):
        return existing
    if context.mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError:
            return None

    if mesh_objects is None:
        if active_object.type == "MESH":
            mesh_objects = [active_object]
        else:
            objects = (
                active_object.users_collection[0].all_objects
                if active_object.users_collection
                else active_object.children
            )
            mesh_objects = [
                obj
                for obj in objects
                if obj.type == "MESH"
                and (
                    obj.parent == active_object
                    or any(
                        modifier.type == "ARMATURE"
                        and modifier.object == active_object
                        for modifier in obj.modifiers
                    )
                )
            ]

    depsgraph = context.evaluated_depsgraph_get()
    points = []
    for mesh_object in mesh_objects or ():
        if mesh_object is None or mesh_object.type != "MESH":
            continue
        evaluated = mesh_object.evaluated_get(depsgraph)
        evaluated_mesh = evaluated.to_mesh()
        try:
            points.extend(
                evaluated.matrix_world @ vertex.co
                for vertex in evaluated_mesh.vertices
            )
        finally:
            evaluated.to_mesh_clear()
    if not points:
        return None

    minimum = Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
    maximum = Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    clearance = max(0.15, max(maximum - minimum) * 0.08)
    target = Vector((
        (minimum.x + maximum.x) * 0.5,
        (minimum.y + maximum.y) * 0.5,
        minimum.z - clearance,
    ))

    source_world = active_object.matrix_world.copy()
    prior_parent = active_object.parent
    root = bpy.data.objects.new(f"{active_object.name}_PresentationRoot", None)
    root.empty_display_type = "CIRCLE"
    root.empty_display_size = max(0.15, clearance)
    collection = (
        active_object.users_collection[0]
        if active_object.users_collection
        else context.collection
    )
    collection.objects.link(root)
    root.parent = prior_parent
    root.matrix_world = Matrix.Translation(target)
    active_object.parent = root
    active_object.matrix_world = source_world

    root["mcbedrock_presentation_root"] = True
    root["mcbedrock_source_object"] = active_object.name
    root["mcbedrock_transform_clearance"] = clearance
    active_object["mcbedrock_transform_origin"] = "PRESENTATION_ROOT"
    active_object["mcbedrock_transform_clearance"] = clearance
    active_object["mcbedrock_presentation_root"] = root.name

    deselect_all_objects(context)
    root.select_set(True)
    context.view_layer.objects.active = root
    context.view_layer.update()
    return root


def place_transform_origin_below_model(context, active_object, mesh_objects=None):
    """Compatibility entry point for the non-destructive wrapper contract."""
    return create_presentation_root(context, active_object, mesh_objects)
