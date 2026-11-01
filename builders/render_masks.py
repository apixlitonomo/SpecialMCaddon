"""Photoshop masks: with every render, save the layers render artists paint with.

Minecraft key art is finished in Photoshop: artists clip highlight and shadow
paint to masks from a bright light, select characters by ID, relight with
normals and fade with depth. This adds a mask branch to the scene's
compositor and a File Output node that writes each mask as its own PNG:

* Light / Shadow: where the lights reach (Diffuse Direct), as a clipping mask.
* Normals: camera-space normals (X right, Y up, Z toward the camera), 0..1.
* Depth: near white to far black over the visible scene.
* Rim / Edge lights / Ink: Sparkle outline layers, when outlines are on.
* ID <name>: one Cryptomatte mask per character, rig or model.
* Block colors: one flat color per kind of block (oak leaves, oak log, grass
  block ...), for Photoshop's Magic Wand: with Contiguous on, one click picks
  the connected blocks of that kind, such as one tree's leaves.

Block colors are the one thing written from materials: each gets a tagged
color and AOV Output that change nothing it renders. Everything it adds is
tagged and removed by disable().
"""
import json

import bpy

TAG = 'mc_render_masks'
OUTPUT = 'Sparkle masks'
PASSES = ('use_pass_diffuse_direct', 'use_pass_normal', 'use_pass_z', 'use_pass_cryptomatte_object', 'use_pass_shadow')
MAX_IDS = 12
BLOCK_PASS = 'Sparkle block colors'
BLOCK_NODES = ('Sparkle block color', 'Sparkle block color output')
# Texture names say which face of a block they cover; the block is the rest.
FACE_SUFFIXES = ('_top', '_bottom', '_side', '_front', '_back', '_end', '_inner', '_outer',
                 '_overlay', '_snow', '_stage0', '_stage1', '_stage2', '_stage3')


def _settings(scene):
    return scene.mcbedrock_masks


def _tree(scene):
    """The scene's compositor; a pass-through one is made if there is none."""
    tree = scene.compositing_node_group
    if tree is None:
        tree = bpy.data.node_groups.new('Sparkle Compositing', 'CompositorNodeTree')
        tree[TAG] = True
        tree.interface.new_socket('Image', in_out='OUTPUT', socket_type='NodeSocketColor')
        render = tree.nodes.new('CompositorNodeRLayers')
        output = tree.nodes.new('NodeGroupOutput')
        tree.links.new(render.outputs['Image'], output.inputs['Image'])
        scene.compositing_node_group = tree
        scene.render.use_compositing = True
    return tree


def _meshes(obj):
    return [o for o in [obj, *obj.children_recursive] if o.type == 'MESH' and not o.hide_render and o.visible_get()
            and o.data is not None and len(o.data.polygons) and not getattr(o, 'is_shadow_catcher', False)
            and not getattr(o, 'is_holdout', False)]


def groups(scene):
    """(name, mesh names) for each character, rig or model: one ID mask each.

    A parented rig or model is one group. Loose parts that share a collection
    (models imported as separate pieces) are grouped by that collection.
    Imported worlds stay out; they are selected by color in Photoshop.
    """
    found, seen, by_collection = [], set(), {}
    for obj in scene.objects:
        if obj.parent is not None or obj.hide_render:
            continue
        if any(c.get('mcaddon_miex_export') for c in obj.users_collection):
            continue
        meshes = [m.name for m in _meshes(obj) if m.name not in seen]
        if not meshes:
            continue
        rig = next((o for o in [obj, *obj.children_recursive] if o.type == 'ARMATURE'), None)
        label = (rig or obj).name.removesuffix('_PresentationRoot').removeprefix('minecraft_')
        collection = next((c for c in obj.users_collection if c != scene.collection), None)
        if obj.type == 'MESH' and not obj.children and collection is not None:
            by_collection.setdefault(collection.name, []).extend(meshes)
        else:
            found.append((label, meshes))
        seen.update(meshes)
    found += [(name, meshes) for name, meshes in by_collection.items()]
    return found[:MAX_IDS]


def separated_groups(scene):
    """(name, [object name]) for each piece Separate Block split off an imported world.

    World objects get no ID mask of their own; a piece the artist split off did so
    to be worked on alone, in Blender and in Photoshop. Point-cloud pieces draw
    their block through Geometry Nodes, and Cryptomatte names those after the cloud."""
    from ..ui.world_blocks import SEPARATED
    return [(obj.name, [obj.name]) for obj in scene.objects
            if obj.get(SEPARATED) and obj.type in {'MESH', 'POINTCLOUD'} and not obj.hide_render and obj.visible_get()]


def id_groups(scene):
    """One ID mask each: characters and models first, then separated world pieces."""
    return (groups(scene) + separated_groups(scene))[:MAX_IDS]


def block_name(name):
    """The block a texture or material draws: ``e803429d9be89bb1_grass_block_side.png``
    and ``MAT_minecraft_block_grass_block_side_8a3dafec.002`` are ``grass_block``."""
    import re
    name = re.sub(r'\.\d{3}$', '', name)
    name = re.sub(r'\.(png|tga|jpe?g|exr)$', '', name, flags=re.I)
    name = re.sub(r'^[0-9a-f]{16}_', '', name)
    name = re.sub(r'_[0-9a-f]{7,}$', '', name)
    changed = True
    while changed:
        changed = False
        for suffix in FACE_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                name, changed = name[:-len(suffix)], True
    return name


def block_color(name):
    """A saturated color fixed by the block's name, far from black and the sky."""
    import colorsys
    import hashlib
    digest = hashlib.md5(name.encode('utf-8')).digest()
    hue = digest[0] / 255
    saturation = .55 + .45 * digest[1] / 255
    value = .45 + .55 * digest[2] / 255
    return colorsys.hsv_to_rgb(hue, saturation, value)


def material_block(material):
    """The block a material draws, from its first texture's file (MiEx cuts long
    material names from the front, so a block's faces can differ there)."""
    image = next((n.image for n in material.node_tree.nodes
                  if n.bl_idname == 'ShaderNodeTexImage' and n.image is not None), None)
    source = bpy.path.basename(image.filepath) if image is not None and image.filepath else ''
    return block_name(source or material.name)


def _scene_materials(scene):
    """Materials the render can show, instanced block models included."""
    found = set()
    for obj in scene.objects:
        for slot in getattr(obj, 'material_slots', ()):
            if slot.material is not None:
                found.add(slot.material)
        data = getattr(obj, 'data', None)
        for material in getattr(data, 'materials', ()) or ():
            if material is not None:
                found.add(material)
    return [m for m in found if m.node_tree is not None and not m.library]


def add_block_colors(scene):
    """Give every material its block's color on the Block colors pass (missing ones only)."""
    for material in _scene_materials(scene):
        nodes = material.node_tree.nodes
        if nodes.get(BLOCK_NODES[1]) is not None:
            continue
        color = nodes.new('ShaderNodeRGB')
        color.name = color.label = BLOCK_NODES[0]
        color.outputs[0].default_value = (*block_color(material_block(material)), 1.)
        output = nodes.new('ShaderNodeOutputAOV')
        output.name = output.label = BLOCK_NODES[1]
        output.aov_name = BLOCK_PASS
        material.node_tree.links.new(color.outputs[0], output.inputs['Color'])
        for node in (color, output):
            node[TAG] = True
            node.location = (-600, -600 - 200 * (node is output))


def remove_block_colors():
    for material in bpy.data.materials:
        if material.node_tree is None or material.library:
            continue
        for name in BLOCK_NODES:
            node = material.node_tree.nodes.get(name)
            if node is not None and node.get(TAG):
                material.node_tree.nodes.remove(node)


def light_reference(scene):
    """Diffuse Direct on a surface squarely facing the brightest key light.

    A sun of strength S gives S / pi; point, spot and area lights are measured
    at the middle of what the masks cover.
    """
    import math
    from mathutils import Vector
    luminance = lambda c: .2126 * c[0] + .7152 * c[1] + .0722 * c[2]
    names = {name for _, meshes in groups(scene) for name in meshes}
    objects = [scene.objects[n] for n in names if n in scene.objects]
    centre = (sum((o.matrix_world.translation for o in objects), Vector()) / len(objects)) if objects else Vector()
    best = 0.
    for obj in scene.objects:
        if obj.type != 'LIGHT' or obj.hide_render or obj.data.energy <= 0:
            continue
        data = obj.data
        strength = data.energy * 2 ** getattr(data, 'exposure', 0.) * luminance(data.color)
        if data.type == 'SUN':
            value = strength / math.pi
        else:
            distance2 = max((obj.matrix_world.translation - centre).length_squared, 1e-4)
            value = strength / (4 * math.pi * math.pi * distance2)
        best = max(best, value)
    return best or 1 / math.pi


def key_direction(scene):
    """Unit vector toward the key light: the brightest sun, else the brightest light."""
    from mathutils import Vector
    names = {name for _, meshes in groups(scene) for name in meshes}
    objects = [scene.objects[n] for n in names if n in scene.objects]
    centre = (sum((o.matrix_world.translation for o in objects), Vector()) / len(objects)) if objects else Vector()
    best, direction = -1., Vector((0, 0, 1))
    for obj in scene.objects:
        if obj.type != 'LIGHT' or obj.hide_render or obj.data.energy <= 0:
            continue
        data = obj.data
        if data.type == 'SUN':
            weight = data.energy * 1e6       # suns win: they set the shadows
            toward = obj.matrix_world.to_3x3() @ Vector((0, 0, 1))
        else:
            offset = obj.matrix_world.translation - centre
            weight = data.energy / max(offset.length_squared, 1e-4)
            toward = offset
        if weight > best and toward.length > 0:
            best, direction = weight, toward.normalized()
    return tuple(direction)


def _camera_axes(scene):
    camera = scene.camera
    if camera is None:
        return (1, 0, 0), (0, 1, 0), (0, 0, 1)
    m = camera.matrix_world.to_3x3().normalized()
    return tuple(m.col[0]), tuple(m.col[1]), tuple(m.col[2])


def _depth_range(scene):
    """Near and far over the characters (or every mesh): the range worth seeing."""
    from mathutils import Vector
    camera = scene.camera
    if camera is None:
        return .1, 100.
    eye = camera.matrix_world.translation
    forward = -(camera.matrix_world.to_3x3() @ Vector((0, 0, 1))).normalized()
    names = {name for _, meshes in groups(scene) for name in meshes}
    objects = [scene.objects[n] for n in names if n in scene.objects] or         [o for o in scene.objects if o.type == 'MESH' and not o.hide_render]
    near, far = None, 0.
    for obj in objects:
        for corner in obj.bound_box:
            depth = (obj.matrix_world @ Vector(corner) - eye).dot(forward)
            if depth > 0:
                near = depth if near is None else min(near, depth)
                far = max(far, depth)
    if near is None:
        return .1, 100.
    return near, max(far, near + 1e-3)


def enable(scene):
    from .lighting_pipeline import Graph
    from . import outlines
    s = _settings(scene)
    disable(scene, keep_setting=True)
    tree = _tree(scene)
    enabled = []
    for layer in scene.view_layers:
        for name in PASSES:
            if not getattr(layer, name):
                setattr(layer, name, True)
                enabled.append((layer.name, name))
        if outlines.outlined_objects(scene) and not any(a.name == outlines.LINE_PASS for a in layer.aovs):
            aov = layer.aovs.add()
            aov.name, aov.type = outlines.LINE_PASS, 'COLOR'
            enabled.append((layer.name, 'aov'))
        if not any(a.name == BLOCK_PASS for a in layer.aovs):
            aov = layer.aovs.add()
            aov.name, aov.type = BLOCK_PASS, 'COLOR'
            enabled.append((layer.name, 'block aov'))
        layer.update_render_passes()
    add_block_colors(scene)
    s.enabled_passes = json.dumps(enabled)
    before = set(tree.nodes)
    render = tree.nodes.new('CompositorNodeRLayers')
    render.name = 'Sparkle mask passes'
    render.scene = scene
    g = Graph(tree)
    # Coverage: the Alpha pass with a transparent film; with an opaque one
    # (a visible background) the normals mark where geometry is, so masks
    # never spill into the sky.
    length = tree.nodes.new('ShaderNodeVectorMath')
    length.operation = 'LENGTH'
    tree.links.new(render.outputs['Normal'], length.inputs[0])
    alpha = g.op('MINIMUM', render.outputs['Alpha'], g.op('MINIMUM', length.outputs['Value'], 1))
    output = tree.nodes.new('CompositorNodeOutputFile')
    output.name = OUTPUT
    if hasattr(output.format, 'media_type'):
        output.format.media_type = 'IMAGE'   # 5.x: single images, one file per mask
    output.format.file_format = 'PNG'
    output.format.color_mode = 'RGBA'
    output.format.color_depth = '16'
    output.save_as_render = False
    output.directory = s.folder
    output.file_name = s.prefix

    def write(name, color, opacity=None):
        item = output.file_output_items.new('RGBA', name)
        image = g.combine(g.split(color), opacity if opacity is not None else alpha)
        tree.links.new(image, output.inputs[name])

    def gray(value):
        return g.combine([value, value, value])

    # Light and shadow: where the key light reaches, crisp enough to clip
    # paint to. A face counts as lit when it turns toward the key light by
    # at least Light threshold (N.L) and is not in shadow. EEVEE gives the
    # shadow directly (its diffuse light also holds the world, so it cannot
    # tell shadow from fill); Cycles' direct light is just the lights.
    threshold = tree.nodes.new('ShaderNodeValue')
    threshold.name = 'Sparkle mask light threshold'
    softness = tree.nodes.new('ShaderNodeValue')
    softness.name = 'Sparkle mask edge'
    key = tree.nodes.new('ShaderNodeCombineXYZ')
    key.name = 'Sparkle mask key direction'
    facing = tree.nodes.new('ShaderNodeVectorMath')
    facing.operation = 'DOT_PRODUCT'
    tree.links.new(render.outputs['Normal'], facing.inputs[0])
    tree.links.new(key.outputs[0], facing.inputs[1])
    shadow = render.outputs.get('Shadow')
    if shadow is not None and shadow.enabled:
        visible = g.split(shadow)[0]
        output['mc_mask_source'] = 'SHADOW'
    else:
        reference = tree.nodes.new('ShaderNodeValue')
        reference.name = 'Sparkle mask light reference'
        direct = g.split(render.outputs['Diffuse Direct'])
        brightness = g.op('ADD', g.op('ADD', g.op('MULTIPLY', direct[0], .2126), g.op('MULTIPLY', direct[1], .7152)),
                          g.op('MULTIPLY', direct[2], .0722))
        # Direct light over what an unblocked face at this angle would get:
        # 1 where the key arrives, 0 in shadow.
        expected = g.op('MAXIMUM', g.op('MULTIPLY', reference.outputs[0], facing.outputs['Value']), 1e-6)
        visible = g.op('MINIMUM', g.op('DIVIDE', brightness, expected), 1)
        output['mc_mask_source'] = 'DIRECT'
    width = g.op('MAXIMUM', g.op('MULTIPLY', softness.outputs[0], 2), 1e-4)
    turned = g.op('DIVIDE', g.op('SUBTRACT', facing.outputs['Value'],
                                 g.op('SUBTRACT', threshold.outputs[0], softness.outputs[0])), width)
    unblocked = g.op('DIVIDE', g.op('SUBTRACT', visible, g.op('SUBTRACT', .5, softness.outputs[0])), width)
    lit = g.op('MULTIPLY', g.op('MINIMUM', g.op('MAXIMUM', turned, 0), 1),
               g.op('MINIMUM', g.op('MAXIMUM', unblocked, 0), 1))
    output['mc_mask_engine'] = scene.render.engine
    write('Light', gray(lit), g.op('MULTIPLY', alpha, lit))
    write('Shadow', gray(g.op('SUBTRACT', 1, lit)), g.op('MULTIPLY', alpha, g.op('SUBTRACT', 1, lit)))
    # Camera-space normals from the world-space Normal pass and the camera's axes.
    axes = []
    for axis in ('right', 'up', 'back'):
        node = tree.nodes.new('ShaderNodeCombineXYZ')
        node.name = 'Sparkle mask camera ' + axis
        axes.append(node.outputs[0])
    normal = render.outputs['Normal']
    parts = []
    for axis in axes:
        dot = tree.nodes.new('ShaderNodeVectorMath')
        dot.operation = 'DOT_PRODUCT'
        tree.links.new(normal, dot.inputs[0])
        tree.links.new(axis, dot.inputs[1])
        parts.append(g.op('ADD', g.op('MULTIPLY', dot.outputs['Value'], .5), .5))
    write('Normals', g.combine(parts))
    # Depth: near white to far black over what is in front of the camera.
    near = tree.nodes.new('ShaderNodeValue')
    near.name = 'Sparkle mask near'
    far = tree.nodes.new('ShaderNodeValue')
    far.name = 'Sparkle mask far'
    span = g.op('MAXIMUM', g.op('SUBTRACT', far.outputs[0], near.outputs[0]), 1e-4)
    depth = g.op('SUBTRACT', 1, g.op('MINIMUM', g.op('MAXIMUM', g.op('DIVIDE', g.op(
        'SUBTRACT', render.outputs['Depth'], near.outputs[0]), span), 0), 1))
    write('Depth', gray(depth))
    # Sparkle outline layers.
    lines = render.outputs.get(outlines.LINE_PASS)
    if lines is not None and lines.enabled:
        rim, edge, ink = g.split(lines)
        for name, value in (('Rim', rim), ('Edge lights', edge), ('Ink', ink)):
            write(name, gray(value), g.op('MULTIPLY', alpha, value))
    # One ID mask per character or model.
    for index, (name, meshes) in enumerate(id_groups(scene) if s.ids else ()):
        crypto = tree.nodes.new('CompositorNodeCryptomatteV2')
        crypto.source = 'RENDER'
        crypto.scene = scene
        crypto.layer_name = scene.view_layers[0].name + '.CryptoObject'
        crypto.matte_id = ','.join(meshes)
        tree.links.new(render.outputs['Image'], crypto.inputs['Image'])
        matte = crypto.outputs['Matte']
        write('ID ' + bpy.path.clean_name(name), gray(matte), matte)
    # Flat block colors, solid where there is geometry.
    blocks = render.outputs.get(BLOCK_PASS)
    if blocks is not None:
        write('Block colors', blocks, alpha)
    for node in set(tree.nodes) - before:
        node[TAG] = True
    s.active = True
    update(scene)


def update(scene):
    from .lighting_pipeline import set_value
    s = _settings(scene)
    tree = scene.compositing_node_group
    if not s.active or tree is None:
        return
    nodes = tree.nodes
    output = nodes.get(OUTPUT)
    if output is None:
        return
    set_value(output, 'directory', s.folder)
    set_value(output, 'file_name', s.prefix)
    set_value(output, 'mute', not s.save)
    if output.get('mc_mask_engine') not in (None, scene.render.engine):
        enable(scene)
        return
    add_block_colors(scene)   # models added since the masks were turned on
    key = nodes.get('Sparkle mask key direction')
    if key is not None:
        for socket, value in zip(key.inputs, key_direction(scene)):
            set_value(socket, 'default_value', float(value))
    for name, value in (('Sparkle mask light threshold', s.light_threshold),
                        ('Sparkle mask edge', .5 * s.edge_softness * min(s.light_threshold, 1 - s.light_threshold, .5)),
                        ('Sparkle mask light reference', light_reference(scene))):
        node = nodes.get(name)
        if node is not None:
            set_value(node.outputs[0], 'default_value', float(value))
    for axis, vector in zip(('right', 'up', 'back'), _camera_axes(scene)):
        node = nodes.get('Sparkle mask camera ' + axis)
        if node is not None:
            for socket, value in zip(node.inputs, vector):
                set_value(socket, 'default_value', float(value))
    near, far = _depth_range(scene)
    for name, value in (('Sparkle mask near', near), ('Sparkle mask far', far)):
        node = nodes.get(name)
        if node is not None:
            set_value(node.outputs[0], 'default_value', float(value))


def disable(scene, keep_setting=False):
    s = _settings(scene)
    tree = scene.compositing_node_group
    if tree is not None:
        for node in [n for n in tree.nodes if n.get(TAG)]:
            tree.nodes.remove(node)
        if tree.get(TAG) and not keep_setting:
            scene.compositing_node_group = None
            bpy.data.node_groups.remove(tree)
    try:
        enabled = json.loads(s.enabled_passes or '[]')
    except ValueError:
        enabled = []
    if not keep_setting:
        for layer_name, name in enabled:
            layer = scene.view_layers.get(layer_name)
            if layer is None:
                continue
            if name in ('aov', 'block aov'):
                from .outlines import LINE_PASS
                wanted = LINE_PASS if name == 'aov' else BLOCK_PASS
                aov = next((a for a in layer.aovs if a.name == wanted), None)
                if aov is not None:
                    layer.aovs.remove(aov)
            else:
                setattr(layer, name, False)
        s.enabled_passes = ''
        s.active = False
        remove_block_colors()


@bpy.app.handlers.persistent
def render_pre(scene, *args):
    if getattr(scene, 'mcbedrock_masks', None) and scene.mcbedrock_masks.active:
        update(scene)


def _changed(self, context):
    update(self.id_data)


def _ids_changed(self, context):
    if self.active:
        enable(self.id_data)


class MaskSettings(bpy.types.PropertyGroup):
    active: bpy.props.BoolProperty(options={'HIDDEN'})
    enabled_passes: bpy.props.StringProperty(options={'HIDDEN'})
    save: bpy.props.BoolProperty(name='Save with each render', default=True, update=_changed)
    folder: bpy.props.StringProperty(name='Folder', subtype='DIR_PATH', default='//masks/', update=_changed)
    prefix: bpy.props.StringProperty(name='File name', default='', update=_changed,
        description='Put before each mask name, such as the shot name')
    ids: bpy.props.BoolProperty(name='Character masks', default=True, update=_ids_changed,
        description='One mask per character, rig or model in the scene')
    light_threshold: bpy.props.FloatProperty(name='Light threshold', default=.15, min=.01, max=.99, update=_changed,
        description='How squarely a face must turn toward the key light to count as lit (0 side-on, 1 head-on)')
    edge_softness: bpy.props.FloatProperty(name='Mask edge softness', default=.15, min=0, max=1, update=_changed,
        description='0 cuts the light and shadow masks as sharp as the pixels allow; 1 follows the soft shadow edge')


class MCBEDROCK_OT_render_masks(bpy.types.Operator):
    bl_idname = 'mcbedrock.render_masks'
    bl_label = 'Photoshop Masks'
    bl_description = 'Save light, shadow, normal, depth, line, character and block color masks with each render'
    bl_options = {'REGISTER', 'UNDO'}
    enable: bpy.props.BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        if self.enable:
            enable(context.scene)
        else:
            disable(context.scene)
        return {'FINISHED'}


CLASSES = (MaskSettings, MCBEDROCK_OT_render_masks)


def register():
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass
    bpy.types.Scene.mcbedrock_masks = bpy.props.PointerProperty(type=MaskSettings)
    if render_pre not in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.append(render_pre)


def unregister():
    if render_pre in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.remove(render_pre)
    if hasattr(bpy.types.Scene, 'mcbedrock_masks'):
        del bpy.types.Scene.mcbedrock_masks
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
