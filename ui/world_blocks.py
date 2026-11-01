"""Separate one piece of an imported world into its own object.

A MiEx world import merges a lot into single objects. Every Individual block of
a chunk (grass, flowers, plants) is one point cloud: each point is a block and
Geometry Nodes put that block's model on it. Every other surface is one mesh per
texture, so all placements of a custom model (every pumpkin patch in the area)
are one mesh with its origin at the chunk corner. Clicking one selects them
all, and moving it moves them all.

Separate Block (right-click menu) makes the clicked piece an object of its own,
with its own data and its origin on it, so it moves alone and can go to its own
render pass: an Individual block's point, or the faces of a mesh that touch the
clicked face (one model placement, or one connected part of the build).
"""
import re

import bpy
import numpy as np
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector

# How far (in Blender units) a hit instance may sit from its point.
TOLERANCE = 1e-3
# Faces closer than this (Blender units) belong to the same piece.
TOUCH = 1e-3
# Marks an object Separate Block made; Photoshop masks give each one its own ID mask.
SEPARATED = 'mcbedrock_separated_block'


def piece_name(name):
    """A readable model name: ``minecraft_custom_furniture_halloween_pumpkin_patch_e6dd06d.001``
    becomes ``furniture_halloween_pumpkin_patch``."""
    name = re.sub(r'\.\d{3}$', '', name)
    name = re.sub(r'_[0-9a-f]{7}$', '', name)
    for prefix in ('minecraft_custom_', 'minecraft_block_', 'minecraft_'):
        if name.startswith(prefix):
            return name[len(prefix):] or name
    return name


def is_block_cloud(obj):
    """A point cloud whose points each place one block model (an Individual-blocks import)."""
    return (obj is not None and obj.type == 'POINTCLOUD'
            and obj.data.attributes.get('proto_index') is not None
            and any(m.type == 'NODES' for m in obj.modifiers))


def _positions(cloud):
    count = len(cloud.data.points)
    values = np.empty(count * 3, np.float32)
    cloud.data.attributes['position'].data.foreach_get('vector', values)
    return values.reshape(count, 3)


def find_block(context, origin, direction):
    """(cloud, point index) of the Individual block a ray hits first, or None."""
    depsgraph = context.evaluated_depsgraph_get()
    hit, _location, _normal, _index, _obj, matrix = context.scene.ray_cast(depsgraph, origin, direction)
    if not hit:
        return None
    where = np.array(matrix.translation, np.float64)
    best = None
    for cloud in context.view_layer.objects:
        if not is_block_cloud(cloud) or not cloud.visible_get() or not len(cloud.data.points):
            continue
        world = np.asarray(cloud.matrix_world, np.float64)
        points = _positions(cloud).astype(np.float64) @ world[:3, :3].T + world[:3, 3]
        distances = np.linalg.norm(points - where, axis=1)
        index = int(np.argmin(distances))
        if distances[index] <= TOLERANCE and (best is None or distances[index] < best[2]):
            best = cloud, index, distances[index]
    return best[:2] if best else None


def pick(context, origin, direction):
    """What a ray hits first: ('BLOCK', cloud, point), ('PIECE', mesh object, face) or None."""
    depsgraph = context.evaluated_depsgraph_get()
    hit, _location, _normal, face, obj, matrix = context.scene.ray_cast(depsgraph, origin, direction)
    if not hit:
        return None
    found = find_block(context, origin, direction)
    if found is not None:
        return ('BLOCK',) + tuple(found)
    original = obj.original if obj else None
    if (original is not None and original.type == 'MESH' and original.name in context.view_layer.objects
            and not original.modifiers and _same(original.matrix_world, matrix)):
        return 'PIECE', original, face
    return None


def _same(a, b):
    return all(abs(x - y) < 1e-5 for row_a, row_b in zip(a, b) for x, y in zip(row_a, row_b))


def touching_faces(obj, start):
    """Indices of the faces connected to face ``start`` by touching or overlapping.

    MiEx writes every face as its own quad, so shared vertices say nothing:
    faces whose bounds touch (in world space, within TOUCH) are one piece."""
    mesh = obj.data
    count = len(mesh.vertices)
    co = np.empty(count * 3, np.float64)
    mesh.vertices.foreach_get('co', co)
    world = np.asarray(obj.matrix_world, np.float64)
    co = co.reshape(count, 3) @ world[:3, :3].T + world[:3, 3]
    starts = np.empty(len(mesh.polygons), np.int64)
    mesh.polygons.foreach_get('loop_start', starts)
    loops = np.empty(len(mesh.loops), np.int64)
    mesh.loops.foreach_get('vertex_index', loops)
    corners = co[loops]
    low = np.minimum.reduceat(corners, starts) - TOUCH
    high = np.maximum.reduceat(corners, starts) + TOUCH
    # Faces bucketed by the grid cells their bounds cover, so each check stays local.
    size = max(float(np.median(np.max(high - low, axis=1))) * 2, 1e-3)
    first = np.floor(low / size).astype(np.int64)
    last = np.floor(high / size).astype(np.int64)

    def covered(index):
        (x0, y0, z0), (x1, y1, z1) = first[index], last[index]
        return [(x, y, z) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1) for z in range(z0, z1 + 1)]
    cells = {}
    for index in range(len(low)):
        for cell in covered(index):
            cells.setdefault(cell, []).append(index)
    piece, stack = {start}, [start]
    while stack:
        index = stack.pop()
        for cell in covered(index):
            for other in cells.get(cell, ()):
                if (other not in piece and np.all(low[other] <= high[index])
                        and np.all(low[index] <= high[other])):
                    piece.add(other)
                    stack.append(other)
    return sorted(piece)


def separate_piece(context, obj, face):
    """Move the faces touching ``face`` into a new object of their own; return it."""
    faces = touching_faces(obj, face)
    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for other in context.selected_objects:
        other.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    if len(faces) < len(obj.data.polygons):
        # Faces, their vertices and edges: Edit Mode rebuilds selection from vertices.
        mesh = obj.data
        flags = np.zeros(len(mesh.polygons), bool)
        flags[faces] = True
        loops = np.empty(len(mesh.loops), np.int64)
        mesh.loops.foreach_get('vertex_index', loops)
        totals = np.empty(len(mesh.polygons), np.int64)
        mesh.polygons.foreach_get('loop_total', totals)
        chosen = np.zeros(len(mesh.vertices), bool)
        chosen[loops[np.repeat(flags, totals)]] = True
        edges = np.empty(len(mesh.edges) * 2, np.int64)
        mesh.edges.foreach_get('vertices', edges)
        mesh.vertices.foreach_set('select', chosen)
        mesh.edges.foreach_set('select', chosen[edges.reshape(-1, 2)].all(axis=1))
        mesh.polygons.foreach_set('select', flags)
        before = set(bpy.data.objects)
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            bpy.ops.mesh.select_mode(type='FACE')
            bpy.ops.mesh.separate(type='SELECTED')
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')
        created = [other for other in set(bpy.data.objects) - before if other.type == 'MESH']
        if len(created) != 1:
            raise RuntimeError('Blender did not separate the piece')
        obj = created[0]
        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
    # A mesh shared with other objects would still move with them in Edit Mode.
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')
    obj.name = obj.data.name = piece_name(obj.name)
    obj[SEPARATED] = True
    return obj


def block_label(cloud, index):
    """The block's model name, from its prototype collection ("017 pumpkin_patch")."""
    proto = cloud.data.attributes['proto_index'].data[index].value
    for modifier in cloud.modifiers:
        group = getattr(modifier, 'node_group', None)
        info = next((n for n in group.nodes if n.bl_idname == 'GeometryNodeCollectionInfo'), None) if group else None
        parent = info.inputs['Collection'].default_value if info else None
        children = sorted(parent.children, key=lambda c: c.name) if parent else []
        if 0 <= proto < len(children):
            name = children[proto].name
            return name.split(' ', 1)[1] if ' ' in name and name.split(' ', 1)[0].isdigit() else name
    return 'block'


def separate_block(context, cloud, index):
    """Move point ``index`` of ``cloud`` into a new object with the cloud's modifiers; return it."""
    count = len(cloud.data.points)
    selection = cloud.data.attributes.get('.selection')
    if selection is None or selection.domain != 'POINT' or selection.data_type != 'BOOLEAN':
        if selection is not None:
            cloud.data.attributes.remove(selection)
        selection = cloud.data.attributes.new('.selection', 'BOOLEAN', 'POINT')
    flags = np.zeros(count, bool)
    flags[index] = True
    selection.data.foreach_set('value', flags)
    position = Vector(_positions(cloud)[index])
    label = block_label(cloud, index)

    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in context.selected_objects:
        obj.select_set(False)
    cloud.select_set(True)
    context.view_layer.objects.active = cloud
    before = set(bpy.data.objects)
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        bpy.ops.pointcloud.separate()
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
    created = [obj for obj in set(bpy.data.objects) - before if obj.type == 'POINTCLOUD']
    if len(created) != 1:
        raise RuntimeError('Blender did not separate the block')
    block = created[0]
    # Origin on the block, so it moves, rotates and scales about itself.
    values = _positions(block) - np.array(position, np.float32)
    block.data.attributes['position'].data.foreach_set('vector', values.ravel())
    block.matrix_world = block.matrix_world @ Matrix.Translation(position)
    block.name = label
    block[SEPARATED] = True
    block.data.name = label
    for obj in context.selected_objects:
        obj.select_set(False)
    block.select_set(True)
    context.view_layer.objects.active = block
    return block


def _view_region(context, event):
    """The 3D View window region under the pointer, with its view."""
    for area in context.window.screen.areas if context.window else ():
        if area.type != 'VIEW_3D':
            continue
        for region in area.regions:
            if (region.type == 'WINDOW' and region.x <= event.mouse_x < region.x + region.width
                    and region.y <= event.mouse_y < region.y + region.height):
                return region, area.spaces.active.region_3d
    return None, None


class MCBEDROCK_OT_separate_world_block(bpy.types.Operator):
    """Click a model or part of an imported world to make it its own object that moves on its own"""
    bl_idname = 'mcbedrock.separate_world_block'
    bl_label = 'Separate Block…'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        context.window.cursor_modal_set('EYEDROPPER')
        context.workspace.status_text_set('Click a block to separate it · Esc or right-click to cancel')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        context.window.cursor_modal_restore()
        context.workspace.status_text_set(None)

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._finish(context)
            return {'CANCELLED'}
        if event.type != 'LEFTMOUSE' or event.value != 'PRESS':
            return {'RUNNING_MODAL'}
        self._finish(context)
        region, view = _view_region(context, event)
        if region is None:
            return {'CANCELLED'}
        point = (event.mouse_x - region.x, event.mouse_y - region.y)
        origin = view3d_utils.region_2d_to_origin_3d(region, view, point)
        direction = view3d_utils.region_2d_to_vector_3d(region, view, point)
        found = pick(context, origin, direction)
        if found is None:
            self.report({'WARNING'}, 'Nothing to separate there. Click a model or block of an imported world')
            return {'CANCELLED'}
        block = (separate_block if found[0] == 'BLOCK' else separate_piece)(context, *found[1:])
        self.report({'INFO'}, f'{block.name} is its own object now: press G to move it')
        return {'FINISHED'}


def _menu(self, context):
    if MCBEDROCK_OT_separate_world_block.poll(context):
        self.layout.separator()
        self.layout.operator(MCBEDROCK_OT_separate_world_block.bl_idname, icon='EYEDROPPER')


def register():
    bpy.utils.register_class(MCBEDROCK_OT_separate_world_block)
    bpy.types.VIEW3D_MT_object_context_menu.append(_menu)


def unregister():
    try:
        bpy.types.VIEW3D_MT_object_context_menu.remove(_menu)
    except Exception:
        pass
    if hasattr(bpy.types, 'MCBEDROCK_OT_separate_world_block'):
        bpy.utils.unregister_class(MCBEDROCK_OT_separate_world_block)
