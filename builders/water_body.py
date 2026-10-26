"""One owned body of water under an import's styled water surfaces.

Water colours light by absorbing and scattering it along the path it travels,
which is how Vibrant Visuals describes its water settings. A Blender volume
computes that path per pixel, in both engines, so no depth map is baked.

Cycles tracks a medium per object. MiEx splits water into chunk objects, and a
ray that entered one chunk and left through the next would stay "in water"
forever, darkening beds along chunk borders. One owned mesh copied from every
styled water face avoids that. Its faces sit a hair inside the surface, so the
two never coincide. Built on explicit application; imported meshes are read,
never changed.
"""
import math

import bpy
import numpy as np
from mathutils import Matrix

TAG = 'mcaddon_water_body'
VERSION = 1
INSET = .002  # blocks
MAX_FACES = 2_000_000
NAMES = {'DUNGEONS': 'MC Dungeons II', 'VIBRANT': 'Vibrant Visuals'}

# Per block (one metre) for red, green and blue light near 650, 550 and 450 nm.
# Vibrant Visuals publishes the three particle concentrations, not their
# optics. These coefficients are Sparkle's choice from standard ocean-optics
# models: pure water (Pope & Fry; Morel), phytoplankton (Bricaud; Gordon &
# Morel), CDOM with a .014/nm slope and mineral sediment.
PURE_WATER_ABSORPTION = (.29, .052, .011)
PURE_WATER_SCATTERING = (.0012, .0019, .0040)
CHLOROPHYLL_ABSORPTION = (.010, .006, .037)   # x chlorophyll ^ .7
CHLOROPHYLL_SCATTERING = (.35, .42, .51)      # x chlorophyll ^ .766
CDOM_ABSORPTION = (.016, .064, .26)           # x CDOM
SEDIMENT_ABSORPTION = (.005, .015, .04)       # x sediment
SEDIMENT_SCATTERING = (.57, .60, .65)         # x sediment
# EEVEE does not dim the light that reaches a surface through a volume, only
# the view path. Twice the extinction stands in for the trip down and back.
EEVEE_LIGHT_PATH = 2.
# Dungeons II is a stylised, nearly opaque body. Its albedo is matched to the
# former diffuse body (water.PRESETS) by render comparison.
DUNGEONS_BODY = (.0004, .036, .032, 1)
DUNGEONS_ALBEDO_SCALE = 6.
DUNGEONS_SHALLOW_TINT = (.45, .75, .8, 1)
DUNGEONS_CLEAR_DEPTH = 3.
# Light crosses the clear depth twice, down to the bed and back up at an
# angle, so the bed is nearly hidden below it. Matched to version 5's opacity.
DUNGEONS_PATH = 1.25


def _shared(collection):
    from .water_foam import _shared_collection
    return _shared_collection(collection)


def owned(collection):
    return [obj for obj in collection.all_objects if obj.get(TAG)]


def remove(collection, *, keep_material=None):
    """Remove this import's body; keep_material survives a rebuild."""
    if _shared(collection):
        return 0
    allowed = {collection, *collection.children_recursive}
    removed, materials = 0, set()
    for obj in owned(collection):
        if any(c not in allowed for c in obj.users_collection):
            for c in list(obj.users_collection):
                if c in allowed:
                    c.objects.unlink(obj)
            continue
        mesh = obj.data
        materials.update(m for m in mesh.materials if m)
        bpy.data.objects.remove(obj, do_unlink=True)
        if not mesh.users and mesh.get(TAG):
            bpy.data.meshes.remove(mesh)
        removed += 1
    for material in materials:
        if material != keep_material and not material.users and material.get(TAG):
            bpy.data.materials.remove(material)
    for key in ('mcaddon_water_body_faces', 'mcaddon_water_body_truncated'):
        if key in collection:
            del collection[key]
    return removed


def _faces(objects, block, inset=INSET, up_only=False):
    """World-space loops of every styled water face, pushed a hair inward.

    A negative inset lifts them out instead; up_only keeps the tops only."""
    from .water import is_water_material
    points, sizes, keys, participating = [], [], [], []
    total = 0
    for obj in objects:
        if obj.type != 'MESH' or obj.get(TAG):
            continue
        slots = [i for i, slot in enumerate(obj.material_slots) if is_water_material(slot.material)]
        mesh = obj.data
        count = len(mesh.polygons)
        if not slots or not count:
            continue
        material = np.empty(count, np.int32)
        starts = np.empty(count, np.int32)
        totals = np.empty(count, np.int32)
        normals = np.empty(count * 3, np.float32)
        mesh.polygons.foreach_get('material_index', material)
        mesh.polygons.foreach_get('loop_start', starts)
        mesh.polygons.foreach_get('loop_total', totals)
        mesh.polygons.foreach_get('normal', normals)
        keep = np.isin(material, slots)
        if up_only:
            up = normals.reshape(-1, 3) @ np.array(obj.matrix_world.to_3x3().inverted_safe(), np.float64)
            keep &= up[:, 2] > .5 * np.maximum(np.linalg.norm(up, axis=1), 1e-12)
        if not keep.any():
            continue
        if total + int(keep.sum()) > MAX_FACES:
            keep[np.flatnonzero(keep)[MAX_FACES - total:]] = False
        starts, totals = starts[keep], totals[keep]
        loops = np.empty(len(mesh.loops), np.int32)
        mesh.loops.foreach_get('vertex_index', loops)
        vertices = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertices.foreach_get('co', vertices)
        first = np.repeat(starts - (np.cumsum(totals) - totals), totals)
        corners = loops[first + np.arange(int(totals.sum()))]
        matrix = np.array(obj.matrix_world, np.float64)
        local = vertices.reshape(-1, 3)[corners].astype(np.float64)
        world = local @ matrix[:3, :3].T + matrix[:3, 3]
        normal = normals.reshape(-1, 3)[keep].astype(np.float64) @ np.linalg.inv(matrix[:3, :3])
        normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-12)
        centers = np.add.reduceat(world, np.cumsum(totals) - totals, axis=0) / totals[:, None]
        world -= np.repeat(normal, totals, axis=0) * (inset * block)
        points.append(world)
        sizes.append(totals)
        keys.append(np.hstack((np.round(centers / (.01 * block)), np.round(normal * 8))).astype(np.int64))
        participating.append(obj)
        total += len(totals)
        if total >= MAX_FACES:
            break
    if not points:
        return None, None, participating, False
    points, sizes, keys = np.concatenate(points), np.concatenate(sizes), np.concatenate(keys)
    # A face copied back to back with its reverse would enter water going
    # down and enter it again coming up. Drop such pairs; one-sided water stays.
    seen = {tuple(k) for k in keys}
    twin = np.array([(k[0], k[1], k[2], -k[3], -k[4], -k[5]) in seen for k in keys.tolist()], bool)
    if twin.any():
        loop_keep = np.repeat(~twin, sizes)
        points, sizes = points[loop_keep], sizes[~twin]
    return points, sizes, participating, total >= MAX_FACES


def _vector(nodes, operation, name, a, b=None, c=None):
    node = nodes.new('ShaderNodeVectorMath')
    node.name = node.label = name
    node.operation = operation
    tree = node.id_data
    for index, source in enumerate((a, b, c)):
        if source is None:
            continue
        if isinstance(source, (tuple, list)):
            node.inputs[index].default_value = source
        else:
            tree.links.new(source, node.inputs[index])
    return node.outputs[0]


def _math(nodes, operation, name, a, b=None):
    node = nodes.new('ShaderNodeMath')
    node.name = node.label = name
    node.operation = operation
    tree = node.id_data
    for index, source in enumerate((a, b)):
        if source is None:
            continue
        if isinstance(source, (int, float)):
            node.inputs[index].default_value = source
        else:
            tree.links.new(source, node.inputs[index])
    return node.outputs[0]


def _value(nodes, name, amount, label=None):
    node = nodes.new('ShaderNodeValue')
    node.name = name
    node.label = label or name
    node.outputs[0].default_value = amount
    return node.outputs[0]


def _vibrant(nodes):
    """Absorption and scattering per block from the official concentrations."""
    chlorophyll = _value(nodes, 'Chlorophyll', 0., 'Chlorophyll (greens the water, 0 to 10)')
    cdom = _value(nodes, 'Dissolved organic matter', 0., 'Dissolved organic matter (yellow-brown, 0 to 15)')
    sediment = _value(nodes, 'Suspended sediment', 0., 'Suspended sediment (red-brown, 0 to 300)')
    absorbed = _math(nodes, 'POWER', 'Chlorophyll absorption scale', chlorophyll, .7)
    scattered = _math(nodes, 'POWER', 'Chlorophyll scattering scale', chlorophyll, .766)
    absorption = _vector(nodes, 'MULTIPLY_ADD', 'Water and chlorophyll absorption',
                         CHLOROPHYLL_ABSORPTION, absorbed, PURE_WATER_ABSORPTION)
    absorption = _vector(nodes, 'MULTIPLY_ADD', 'Organic matter absorption', CDOM_ABSORPTION, cdom, absorption)
    absorption = _vector(nodes, 'MULTIPLY_ADD', 'Absorption per block', SEDIMENT_ABSORPTION, sediment, absorption)
    scattering = _vector(nodes, 'MULTIPLY_ADD', 'Water and chlorophyll scattering',
                         CHLOROPHYLL_SCATTERING, scattered, PURE_WATER_SCATTERING)
    scattering = _vector(nodes, 'MULTIPLY_ADD', 'Scattering per block', SEDIMENT_SCATTERING, sediment, scattering)
    return absorption, scattering


def _dungeons(nodes):
    """A dense lit body; the bed shows through the first few blocks."""
    body = nodes.new('ShaderNodeRGB')
    body.name = body.label = 'Body color'
    body.outputs[0].default_value = DUNGEONS_BODY
    tint = nodes.new('ShaderNodeRGB')
    tint.name = tint.label = 'Shallow tint'
    tint.outputs[0].default_value = DUNGEONS_SHALLOW_TINT
    depth = _value(nodes, 'Clear depth (blocks)', DUNGEONS_CLEAR_DEPTH)
    # Bed light fades to a third, tinted, over the clear depth.
    logarithm = nodes.new('ShaderNodeSeparateColor')
    logarithm.name = logarithm.label = 'Shallow tint channels'
    nodes.id_data.links.new(tint.outputs[0], logarithm.inputs[0])
    extinction = nodes.new('ShaderNodeCombineXYZ')
    extinction.name = extinction.label = 'Shallow fade'
    span = _math(nodes, 'MULTIPLY', 'Clear light path', depth, DUNGEONS_PATH)
    for channel, axis in zip(('Red', 'Green', 'Blue'), 'XYZ'):
        log = _math(nodes, 'LOGARITHM', channel + ' tint log',
                    _math(nodes, 'MAXIMUM', channel + ' tint floor', logarithm.outputs[channel], .001), math.e)
        fade = _math(nodes, 'DIVIDE', channel + ' extinction',
                     _math(nodes, 'SUBTRACT', channel + ' tinted fade', 1., log),
                     _math(nodes, 'MAXIMUM', channel + ' clear path floor', span, .01))
        nodes.id_data.links.new(fade, extinction.inputs[axis])
    albedo = _vector(nodes, 'MINIMUM', 'Body albedo', _vector(
        nodes, 'SCALE', 'Scaled body color', body.outputs[0]), (.95, .95, .95))
    nodes['Scaled body color'].inputs['Scale'].default_value = DUNGEONS_ALBEDO_SCALE
    scattering = _vector(nodes, 'MULTIPLY', 'Scattering per block', extinction.outputs[0], albedo)
    absorption = _vector(nodes, 'SUBTRACT', 'Absorption per block', extinction.outputs[0], scattering)
    return absorption, scattering


def _medium(nodes, links, prefix, absorption, scattering, anisotropy):
    """Volume Absorption + Scatter with exact per-channel coefficients."""
    def largest(vector, name):
        parts = nodes.new('ShaderNodeSeparateXYZ')
        parts.name = parts.label = name + ' channels'
        links.new(vector, parts.inputs[0])
        peak = _math(nodes, 'MAXIMUM', name + ' peak', _math(
            nodes, 'MAXIMUM', name + ' red/green peak', parts.outputs['X'], parts.outputs['Y']), parts.outputs['Z'])
        return _math(nodes, 'MAXIMUM', name + ' density', peak, 1e-6)
    density = largest(absorption, prefix + 'absorption')
    absorb = nodes.new('ShaderNodeVolumeAbsorption')
    absorb.name = absorb.label = prefix + 'Water absorption'
    links.new(density, absorb.inputs['Density'])
    # Blender absorbs density x (1 - color).
    links.new(_vector(nodes, 'SUBTRACT', prefix + 'absorption color', (1, 1, 1), _vector(
        nodes, 'DIVIDE', prefix + 'relative absorption', absorption, density)), absorb.inputs['Color'])
    density = largest(scattering, prefix + 'scattering')
    scatter = nodes.new('ShaderNodeVolumeScatter')
    scatter.name = scatter.label = prefix + 'Water scattering'
    links.new(density, scatter.inputs['Density'])
    links.new(_vector(nodes, 'DIVIDE', prefix + 'scattering color', scattering, density), scatter.inputs['Color'])
    links.new(anisotropy, scatter.inputs['Anisotropy'])
    medium = nodes.new('ShaderNodeAddShader')
    medium.name = medium.label = prefix + 'Water medium'
    links.new(absorb.outputs[0], medium.inputs[0])
    links.new(scatter.outputs[0], medium.inputs[1])
    return medium.outputs[0]


def build_material(preset, block, viewport_color):
    material = bpy.data.materials.new('Sparkle water body · ' + NAMES[preset])
    material[TAG] = preset
    material['mc_edge_exclude'] = material['mc_outline_exclude'] = True
    material.diffuse_color = viewport_color
    material.use_backface_culling = False
    if hasattr(material, 'volume_intersection_method'):
        # Fast intersection counts every face; on an open water sheet EEVEE
        # then tinted banks and props up to a block above the waterline.
        material.volume_intersection_method = 'ACCURATE'
    material.use_nodes = True
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    nodes.clear()
    absorption, scattering = (_vibrant if preset == 'VIBRANT' else _dungeons)(nodes)
    per_unit = _value(nodes, 'Blocks per unit', 1 / block)
    absorption = _vector(nodes, 'SCALE', 'Absorption per unit', absorption)
    links.new(per_unit, nodes['Absorption per unit'].inputs['Scale'])
    scattering = _vector(nodes, 'SCALE', 'Scattering per unit', scattering)
    links.new(per_unit, nodes['Scattering per unit'].inputs['Scale'])
    anisotropy = _value(nodes, 'Forward scattering', .3)
    cycles = nodes.new('ShaderNodeOutputMaterial')
    cycles.name = cycles.label = 'Water body output (Cycles)'
    cycles.target = 'CYCLES'
    links.new(_medium(nodes, links, '', absorption, scattering, anisotropy), cycles.inputs['Volume'])
    light_path = _value(nodes, 'EEVEE light path', EEVEE_LIGHT_PATH)
    extra = _vector(nodes, 'SCALE', 'EEVEE extra extinction', _vector(
        nodes, 'ADD', 'Extinction per unit', absorption, scattering))
    links.new(_math(nodes, 'SUBTRACT', 'EEVEE extra path', light_path, 1.), nodes['EEVEE extra extinction'].inputs['Scale'])
    eevee_absorption = _vector(nodes, 'ADD', 'EEVEE absorption per unit', absorption, extra)
    eevee = nodes.new('ShaderNodeOutputMaterial')
    eevee.name = eevee.label = 'Water body output (EEVEE)'
    eevee.target = 'EEVEE'
    links.new(_medium(nodes, links, 'EEVEE ', eevee_absorption, scattering, anisotropy), eevee.inputs['Volume'])
    cycles.is_active_output = eevee.is_active_output = True
    _arrange(nodes)
    return material


def _arrange(nodes):
    depths, rows = {}, {}
    def depth(item):
        if item not in depths:
            parents = [link.from_node for socket in item.inputs for link in socket.links]
            depths[item] = 1 + max((depth(parent) for parent in parents), default=-1)
        return depths[item]
    for item in nodes:
        column = depth(item)
        row = rows.get(column, 0)
        item.location = (column * 240, -row * 200)
        rows[column] = row + 1


def rebuild(scene, collection, preset, *, water_objects):
    """Copy the styled water faces into one owned volume; report the face count."""
    from .water_foam import _root
    if _shared(collection):
        raise ValueError('This world is shared by multiple scenes; make a separate copy before changing water')
    block = max(.001, float(collection.get('mcaddon_miex_block_units', 1)))
    previous = next((m for obj in owned(collection) for m in obj.data.materials
                     if m and m.get(TAG) == preset), None)
    remove(collection, keep_material=previous)
    points, sizes, participating, truncated = _faces(water_objects, block)
    collection['mcaddon_water_body_faces'] = 0 if sizes is None else len(sizes)
    collection['mcaddon_water_body_truncated'] = truncated
    if sizes is None or not len(sizes):
        if previous is not None and not previous.users:
            bpy.data.materials.remove(previous)
        return dict(faces=0, truncated=truncated)
    mesh = bpy.data.meshes.new('Sparkle water body')
    mesh[TAG] = VERSION
    mesh.vertices.add(len(points))
    mesh.vertices.foreach_set('co', points.astype(np.float32).ravel())
    mesh.loops.add(len(points))
    mesh.loops.foreach_set('vertex_index', np.arange(len(points), dtype=np.int32))
    mesh.polygons.add(len(sizes))
    mesh.polygons.foreach_set('loop_start', (np.cumsum(sizes) - sizes).astype(np.int32))
    mesh.update()
    if previous is None:
        surface = next((slot.material for obj in participating for slot in obj.material_slots
                        if slot.material and slot.material.get('mcaddon_water_preset') == preset), None)
        color = tuple(surface.diffuse_color) if surface else (.2, .45, .65, 1)
        previous = build_material(preset, block, color)
    mesh.materials.append(previous)
    obj = bpy.data.objects.new('Sparkle · Water body', mesh)
    obj[TAG], obj['mc_sky_owner'] = VERSION, 'water_body'
    obj.hide_select = True
    collection.objects.link(obj)
    root = _root(participating)
    if root and root not in participating:
        obj.parent = root
        obj.matrix_world = Matrix.Identity(4)
    return dict(faces=len(sizes), truncated=truncated)
