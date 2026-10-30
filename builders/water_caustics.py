"""Native animated caustic artwork on owned, submerged lakebed overlays.

Receiver patches are baked on explicit water application, never every frame.
Imported geometry/materials stay untouched. Reapply after moving shorelines.
This is an authored-texture projection, not Bedrock's deferred caustics pass.
"""
import math
from pathlib import Path

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

TAG = 'mcaddon_water_caustics'
VERSION = 1
CELL = 1.
LIFT = .002
MAX_DEPTH = 12
MAX_WATER_FACES = 250_000
MAX_RAY_STEPS = 24
MAX_CANDIDATES = 300_000
MAX_PATCHES = 80_000
DOWN = Vector((0, 0, -1))


def remove(collection):
    from .water_foam import _shared_collection
    if _shared_collection(collection):
        return 0
    allowed = {collection, *collection.children_recursive}
    removed, materials, images = 0, set(), set()
    for obj in list(collection.all_objects):
        if not obj.get(TAG):
            continue
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
        if not material.users and material.get(TAG):
            images.update(n.image for n in material.node_tree.nodes if n.type == 'TEX_IMAGE' and n.image)
            bpy.data.materials.remove(material)
    for image in images:
        if not image.users and image.get(TAG):
            bpy.data.images.remove(image)
    for key in ('mcaddon_water_caustics_patches', 'mcaddon_water_caustics_truncated', 'mcaddon_water_caustics_reason'):
        if key in collection:
            del collection[key]
    return removed


def _opaque(material):
    from .water import is_water_material
    if material is None:
        return True
    if is_water_material(material) or material.get(TAG) or material.get('mcaddon_water_foam'):
        return False
    if not material.use_nodes:
        return material.diffuse_color[3] >= .999
    nodes = [n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED']
    if len(nodes) != 1:
        return False
    ports = nodes[0].inputs
    alpha = ports['Alpha']
    opaque = not alpha.is_linked and alpha.default_value >= .999
    if alpha.is_linked and material.get('mc_lighting_version'):
        # Our sky wraps even constant-one alpha in a blue-noise mix. Follow
        # only that known wrapper's original input; mapped cutouts stay out.
        source = alpha.links[0].from_node
        if source.type == 'MIX_RGB' and source.name == 'MC opacity':
            original = source.inputs[1]
            opaque = not original.is_linked and min(original.default_value[:3]) >= .999
    return (opaque and
            not ports['Transmission Weight'].is_linked and ports['Transmission Weight'].default_value < .1)


def _driver(socket, scene, expression, variables):
    driver = socket.driver_add('default_value').driver
    driver.expression = expression
    for name, path in variables:
        variable = driver.variables.new()
        variable.name, variable.type = name, 'SINGLE_PROP'
        variable.targets[0].id_type = 'SCENE'
        variable.targets[0].id = scene
        variable.targets[0].data_path = path


def _material(scene):
    material = bpy.data.materials.new('Sparkle · Vibrant underwater caustics')
    material[TAG], material['mc_edge_exclude'] = VERSION, True
    material.use_nodes = True
    material.use_backface_culling = True
    material.surface_render_method = 'DITHERED'
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()

    def value(name, amount):
        n = nodes.new('ShaderNodeValue')
        n.name = n.label = name
        n.outputs[0].default_value = amount
        return n.outputs[0]

    def op(kind, a, b=None):
        n = nodes.new('ShaderNodeMath')
        n.operation = kind
        for i, source in enumerate((a, b)):
            if source is None:
                continue
            if isinstance(source, (int, float)):
                n.inputs[i].default_value = source
            else:
                links.new(source, n.inputs[i])
        return n.outputs[0]

    uv = nodes.new('ShaderNodeUVMap')
    uv.uv_map = 'Caustic position'
    separate = nodes.new('ShaderNodeSeparateXYZ')
    links.new(uv.outputs['UV'], separate.inputs[0])
    scale = value('Caustics scale', .5)
    u = op('FRACT', op('MULTIPLY', separate.outputs['X'], scale))
    v = op('FRACT', op('MULTIPLY', separate.outputs['Y'], scale))
    seconds = value('Caustics time', 0)
    _driver(seconds, scene, 'frame * fps_base / fps', [('fps', 'render.fps'), ('fps_base', 'render.fps_base')])
    frame = op('MODULO', op('FLOOR', op('DIVIDE', seconds, value('Caustics frame length', .09))), 64)
    atlas_v = op('DIVIDE', op('ADD', op('SUBTRACT', 63, frame), v), 64)
    coords = nodes.new('ShaderNodeCombineXYZ')
    links.new(u, coords.inputs['X'])
    links.new(atlas_v, coords.inputs['Y'])
    texture = nodes.new('ShaderNodeTexImage')
    texture.name = 'Native 64-frame caustics'
    texture.image = bpy.data.images.load(str(Path(__file__).resolve().parents[1] / 'assets/vibrant_visuals/caustics.png'), check_existing=False)
    texture.image[TAG] = VERSION
    texture.image.colorspace_settings.name = 'Non-Color'
    texture.image.pack()
    texture.interpolation, texture.extension = 'Closest', 'EXTEND'
    links.new(coords.outputs[0], texture.inputs['Vector'])
    depth = nodes.new('ShaderNodeAttribute')
    depth.attribute_name = 'mc_caustic_depth'
    attenuation = op('EXPONENT', op('MULTIPLY', depth.outputs['Fac'], -.22))
    light = value('Caustics daylight', 1)
    if hasattr(scene, 'mcbedrock_sky'):
        hour = '(time+24*(frame-start)/max(1,round(duration*fps/fps_base)) if cycle else time)'
        _driver(light, scene, 'min(1,max(0,sin((' + hour + '-6)*pi/12)*3)) if active else 1',
                [('time', 'mcbedrock_sky.time'), ('active', 'mcbedrock_sky.active'),
                 ('cycle', 'mcbedrock_sky.cycle_enabled'), ('start', 'mcbedrock_sky.cycle_start'),
                 ('duration', 'mcbedrock_sky.cycle_seconds'), ('fps', 'render.fps'), ('fps_base', 'render.fps_base')])
    power = op('MULTIPLY', value('Caustics power', 1), light)
    mask = op('MULTIPLY', op('MULTIPLY', texture.outputs['Color'], attenuation), power)
    transparent = nodes.new('ShaderNodeBsdfTransparent')
    emission = nodes.new('ShaderNodeEmission')
    emission.inputs['Color'].default_value = (.8, .94, 1, 1)
    # EEVEE cannot refract additive transparency. A bright emission mix stays
    # in its dithered buffer; the denser mask keeps native pixels readable.
    emission.inputs['Strength'].default_value = 2.
    mix = nodes.new('ShaderNodeMixShader')
    links.new(op('MINIMUM', mask, .65), mix.inputs[0])
    links.new(transparent.outputs[0], mix.inputs[1])
    links.new(emission.outputs[0], mix.inputs[2])
    output = nodes.new('ShaderNodeOutputMaterial')
    output.name, output.target = 'Caustics output (EEVEE)', 'EEVEE'
    links.new(mix.outputs[0], output.inputs['Surface'])
    # Cycles supports true additive light and can preserve the receiver's
    # complete contribution even on exceptionally bright surfaces.
    cycles_emission = nodes.new('ShaderNodeEmission')
    cycles_emission.inputs['Color'].default_value = (.8, .94, 1, 1)
    links.new(op('MULTIPLY', mask, .7), cycles_emission.inputs['Strength'])
    addition = nodes.new('ShaderNodeAddShader')
    links.new(transparent.outputs[0], addition.inputs[0])
    links.new(cycles_emission.outputs[0], addition.inputs[1])
    cycles_output = nodes.new('ShaderNodeOutputMaterial')
    cycles_output.name, cycles_output.target = 'Caustics output (Cycles)', 'CYCLES'
    links.new(addition.outputs[0], cycles_output.inputs['Surface'])
    output.is_active_output = cycles_output.is_active_output = True
    from .water import prepare_atmosphere
    prepare_atmosphere(scene, material)
    return material


def prepare_atmosphere(scene):
    from .water import prepare_atmosphere as prepare
    for material in {slot.material for obj in scene.objects if obj.get(TAG)
                     for slot in obj.material_slots if slot.material and slot.material.get(TAG)}:
        prepare(scene, material)


def rebuild(scene, collection, *, water_objects=None):
    """Bake bounded patches on first upward-facing, opaque submerged receivers.

    Read water meshes and raycast evaluated receivers in world space. Corner checks keep patches
    inside water/receiver footprints and off steps, caves, undersides and land.
    Reapply after editing or moving imported geometry or receiver modifiers.
    """
    from .water import is_water_material
    from .water_foam import _shared_collection, _root
    if not collection or not collection.get('mcaddon_miex_export'):
        raise ValueError('Choose an imported MiEx world collection')
    if collection not in set(scene.collection.children_recursive):
        raise ValueError('The imported world is not in this scene')
    if _shared_collection(collection):
        raise ValueError('This world is shared by multiple scenes; make a separate copy before changing water')
    block = max(.001, float(collection.get('mcaddon_miex_block_units', 1)))
    cell, epsilon = CELL * block, .001 * block
    water_vertices, water_faces, bounds, participating = [], [], [], []
    truncated, reason = False, ''
    sources = list(water_objects) if water_objects is not None else list(collection.all_objects)
    for obj in sources:
        if obj.type != 'MESH' or obj.hide_render or obj.get(TAG):
            continue
        slots = {i for i, slot in enumerate(obj.material_slots) if is_water_material(slot.material)}
        if not slots:
            continue
        matrix = obj.matrix_world
        normals = matrix.to_3x3().inverted_safe().transposed()
        for face in obj.data.polygons:
            if face.material_index not in slots or (normals @ face.normal).normalized().z < .95:
                continue
            points = [matrix @ obj.data.vertices[i].co for i in face.vertices]
            if max(p.z for p in points) - min(p.z for p in points) > .025 * block:
                continue
            offset = len(water_vertices)
            water_vertices.extend(points)
            water_faces.append(tuple(range(offset, len(water_vertices))))
            bounds.append((min(p.x for p in points), max(p.x for p in points), min(p.y for p in points), max(p.y for p in points), sum(p.z for p in points) / len(points)))
            if obj not in participating:
                participating.append(obj)
            if len(water_faces) >= MAX_WATER_FACES:
                truncated, reason = True, 'Water face limit'
                break
        if truncated:
            break

    def finish(patches, why=reason):
        remove(collection)
        collection['mcaddon_water_caustics_patches'] = len(patches)
        collection['mcaddon_water_caustics_truncated'] = truncated
        collection['mcaddon_water_caustics_reason'] = why
        return dict(patches=len(patches), truncated=truncated, reason=why)

    if not water_faces:
        return finish([])
    water = BVHTree.FromPolygons(water_vertices, water_faces)
    # Blender already has an evaluated scene acceleration structure. Reuse it
    # instead of copying millions of terrain faces into another Python BVH.
    view_layer = bpy.context.view_layer if bpy.context.scene == scene else scene.view_layers[0]
    depsgraph = view_layer.depsgraph
    # Water/sky application may just have replaced material slots. Resolve
    # those edits before reading evaluated hit topology and material slots.
    depsgraph.update()
    from .column_rays import ColumnRays, cells, grid_shift
    # Sample each block's centre and corners on the world's own block grid.
    sx, sy = grid_shift([(box[0], box[2]) for box in bounds], cell)
    levels = {}
    for bx0, bx1, by0, by1, level in bounds:
        for x in cells(bx0, bx1, cell, sx):
            for y in cells(by0, by1, cell, sy):
                levels[x, y] = min(level, levels.get((x, y), level))
    rays = ColumnRays(scene, depsgraph, levels, cell, MAX_DEPTH * block + 2 * block, shift=(sx, sy))
    evaluated, opaque_slots = {}, {}
    blocked = object()

    def first_solid(origin, direction, distance):
        nonlocal truncated, reason
        start = origin.copy()
        remaining = distance
        for _ in range(MAX_RAY_STEPS):
            found, location, normal, index, obj, _matrix = rays.ray_cast(
                depsgraph, start, direction, distance=remaining)
            if not found:
                return None
            original = obj.original
            if not (original.hide_render or original.get(TAG) or original.get('mcaddon_water_foam') or original.get('mc_sky_owner')):
                if original not in evaluated:
                    evaluated[original] = original.evaluated_get(depsgraph)
                    opaque_slots[original] = {i for i, slot in enumerate(evaluated[original].material_slots) if _opaque(slot.material)}
                    if not evaluated[original].material_slots:
                        opaque_slots[original].add(0)
                # Ray indices belong to evaluated topology, never the original
                # mesh when modifiers change face counts/material assignment.
                polygons = getattr(evaluated[original].data, 'polygons', None)
                if polygons is None or not 0 <= index < len(polygons):
                    truncated, reason = True, 'Unsupported receiver mapping'
                    return blocked
                if polygons[index].material_index in opaque_slots[original]:
                    return location, normal, (location - origin).length
            step = (location - start).length + epsilon
            remaining -= step
            if remaining <= 0:
                return None
            start = location + direction * epsilon
        # Exhausting a transparent stack cannot authorize painting through it.
        truncated, reason = True, 'Occlusion step limit'
        return blocked

    candidates, patches = set(), {}
    stop = False
    # Prefer exposed lakes before deep pools when a large export reaches its
    # work budget. That budget counts work, never time: the same geometry
    # always bakes the same caustics, on fast and slow computers alike.
    for bx0, bx1, by0, by1, level in sorted(bounds, key=lambda box: -box[4]):
        for x in cells(bx0, bx1, cell, sx):
            for y in cells(by0, by1, cell, sy):
                key = (round(level / block, 4), x, y)
                if key in candidates:
                    continue
                if len(candidates) >= MAX_CANDIDATES:
                    truncated, reason, stop = True, 'Projection sample limit', True
                    break
                candidates.add(key)
                center = Vector(((x + sx + .5) * cell, (y + sy + .5) * cell, level - epsilon))
                # Conservative vertical sky access prevents daytime emission
                # in roofed pools. This is not angular sun-shadow tracing.
                above = Vector((center.x, center.y, level + epsilon))
                if first_solid(above, -DOWN, 1e20) is not None:
                    continue
                receiver_hit = first_solid(center, DOWN, MAX_DEPTH * block)
                if receiver_hit is None or receiver_hit is blocked:
                    continue
                hit, normal, distance = receiver_hit
                if normal.z < .5 or distance < epsilon:
                    continue
                corners = []
                for dx, dy in ((.001, .001), (.999, .001), (.999, .999), (.001, .999)):
                    p = Vector(((x + sx + dx) * cell, (y + sy + dy) * cell, level + .05 * block))
                    wet = water.ray_cast(p, DOWN, .1 * block)[0]
                    if wet is None or abs(wet.z - level) > .025 * block:
                        break
                    p.z = level + epsilon
                    if first_solid(p, -DOWN, 1e20) is not None:
                        break
                    p.z = level - epsilon
                    receiver_hit = first_solid(p, DOWN, MAX_DEPTH * block)
                    if receiver_hit is None or receiver_hit is blocked:
                        break
                    receiver, rn, _ = receiver_hit
                    if rn.dot(normal) < .999 or abs((receiver - hit).dot(normal)) > .001 * block:
                        break
                    corners.append(receiver + normal * LIFT * block)
                if len(corners) != 4:
                    continue
                # Overlapping chunk faces must not double the light pattern.
                patch_key = (x, y, round(hit.z / block, 4))
                depth = (level - hit.z) / block
                if patch_key not in patches or depth < patches[patch_key][1]:
                    patches[patch_key] = (corners, depth)
                if len(patches) >= MAX_PATCHES:
                    truncated, reason, stop = True, 'Receiver patch limit', True
                    break
            if stop:
                break
        if stop:
            break
    result = finish(patches, reason)
    if not patches:
        return result
    verts, quads, depths = [], [], []
    for points, depth in patches.values():
        offset = len(verts)
        verts.extend(points)
        quads.append(tuple(range(offset, offset + 4)))
        depths.extend([depth] * 4)
    mesh = bpy.data.meshes.new('Sparkle submerged caustic receivers')
    mesh[TAG] = VERSION
    mesh.from_pydata(verts, [], quads)
    mesh.update()
    uv = mesh.uv_layers.new(name='Caustic position')
    uv.data.foreach_set('uv', [v for loop in mesh.loops for v in (verts[loop.vertex_index].x / block, verts[loop.vertex_index].y / block)])
    attribute = mesh.attributes.new('mc_caustic_depth', 'FLOAT', 'POINT')
    attribute.data.foreach_set('value', depths)
    obj = bpy.data.objects.new('Sparkle · Underwater caustics', mesh)
    obj[TAG], obj['mc_sky_owner'] = VERSION, 'water_caustics'
    obj.hide_select = True
    # This luminous overlay must not cast a second shadow onto its receiver.
    obj.visible_shadow = False
    collection.objects.link(obj)
    root = _root(participating)
    if root and root not in participating:
        obj.parent = root
        obj.matrix_world = Matrix.Identity(4)
    mesh.materials.append(_material(scene))
    return result
