"""Photoshop-style Stroke and Outer Glow around outlined models, in the compositor.

Outlined materials write two passes: a mask of the models that take a stroke
and the rim light that reaches each one's outline (outlines.py). Blender's
Mask to SDF node turns the mask into exact distances in pixels, so the stroke
can sit outside, across or inside the silhouette at any width with clean
edges. Its lit side takes the rim light at the nearest point of the model, so
a recolored light paints its own side of the stroke. The cost is one distance
transform per frame, whatever the scene holds.
"""
import bpy

from .outlines import STROKE_PASS, STROKE_LIGHT_PASS, set_value, stroke_wanted

GROUP = 'Sparkle Stroke'
NODE = 'Sparkle Stroke'
PASSES = 'Sparkle Stroke passes'
TAG = 'mc_stroke_graph'
OWNED = 'mc_stroke_owned'            # a compositor tree Sparkle created for the stroke
PREVIOUS = 'mc_stroke_previous_compositing'
VERSION = 3
PARAMETERS = (('Size', 5.), ('Position', 0.), ('Opacity', 1.), ('Softness', 0.), ('Light', 1.),
              ('Lit only', 1.), ('Shadow only', 0.), ('Glow size', 0.), ('Glow strength', .6))
COLORS = (('Stroke color', (.012, .008, .01)),)
POSITIONS = {'OUTSIDE': 1., 'CENTER': .5, 'INSIDE': 0.}


class _Build:
    def __init__(self, tree):
        self.tree, self.nodes, self.links = tree, tree.nodes, tree.links

    def new(self, kind, name=None, **properties):
        node = self.nodes.new(kind)
        if name:
            node.name = node.label = name
        for key, value in properties.items():
            setattr(node, key, value)
        return node

    def put(self, socket, value):
        if isinstance(value, bpy.types.NodeSocket):
            self.links.new(value, socket)
        else:
            socket.default_value = value

    def value(self, name, default):
        node = self.new('ShaderNodeValue', name)
        node.outputs[0].default_value = default
        return node.outputs[0]

    def math(self, operation, a, b=0., c=0.):
        node = self.new('ShaderNodeMath', operation=operation)
        for socket, value in zip(node.inputs, (a, b, c)):
            self.put(socket, value)
        return node.outputs[0]

    def mul(self, a, b): return self.math('MULTIPLY', a, b)
    def add(self, a, b): return self.math('ADD', a, b)
    def sub(self, a, b): return self.math('SUBTRACT', a, b)
    def one_minus(self, a): return self.math('SUBTRACT', 1., a)

    def smooth(self, value, low, high):
        node = self.new('ShaderNodeMapRange', interpolation_type='SMOOTHSTEP', clamp=True)
        for name, item in (('Value', value), ('From Min', low), ('From Max', high)):
            self.put(node.inputs[name], item)
        return node.outputs['Result']

    def mix(self, factor, a, b):
        node = self.new('ShaderNodeMix', data_type='RGBA', clamp_result=False)
        self.put(node.inputs[0], factor)
        self.put(node.inputs[6], a)
        self.put(node.inputs[7], b)
        return node.outputs[2]

    def vector(self, operation, a, b=None, scale=None):
        node = self.new('ShaderNodeVectorMath', operation=operation)
        self.put(node.inputs[0], a)
        if b is not None:
            self.put(node.inputs[1], b)
        if scale is not None:
            self.put(node.inputs['Scale'], scale)
        return node.outputs['Value' if operation in ('DOT_PRODUCT', 'LENGTH') else 'Vector']


BLENDS = ('MIX', 'SCREEN', 'OVERLAY', 'SOFT_LIGHT', 'ADD')


def layer(b, base, paint, amount):
    """Photoshop's blend modes, weighted by 'Blend <mode>' values."""
    result = None
    for index, mode in enumerate(BLENDS):
        node = b.new('ShaderNodeMix', data_type='RGBA', blend_type=mode, clamp_result=False)
        b.put(node.inputs[0], amount)
        b.put(node.inputs[6], base)
        b.put(node.inputs[7], paint)
        weight = b.tree.nodes.get('Blend ' + mode)
        weight = weight.outputs[0] if weight is not None else b.value('Blend ' + mode, 1. if index == 0 else 0.)
        part = b.vector('SCALE', node.outputs[2], scale=weight)
        result = part if result is None else b.vector('ADD', result, part)
    return result


def build(tree):
    tree.nodes.clear()
    names = {item.name for item in tree.interface.items_tree if item.item_type == 'SOCKET'}
    if 'Rim color' not in names:
        tree.interface.clear()
        for name, direction, kind in (('Image', 'INPUT', 'NodeSocketColor'), ('Mask', 'INPUT', 'NodeSocketColor'),
                                      ('Rim color', 'INPUT', 'NodeSocketColor'), ('Image', 'OUTPUT', 'NodeSocketColor')):
            tree.interface.new_socket(name=name, in_out=direction, socket_type=kind)
    b = _Build(tree)
    source, out = b.new('NodeGroupInput'), b.new('NodeGroupOutput')
    p = {name: b.value(name, default) for name, default in PARAMETERS}
    color_node = b.new('CompositorNodeRGB', 'Stroke color')
    color_node.outputs[0].default_value = (*COLORS[0][1], 1.)
    image = source.outputs['Image']
    # Mask pass: R = the model's coverage, G = coverage on its lit side.
    coverage = b.new('CompositorNodeSeparateColor')
    b.put(coverage.inputs['Image'], source.outputs['Mask'])
    mask = coverage.outputs['Red']

    info = b.new('CompositorNodeImageInfo')
    b.put(info.inputs['Image'], image)
    size = b.new('ShaderNodeSeparateXYZ')
    b.put(size.inputs[0], info.outputs['Resolution'])
    scale = b.math('DIVIDE', size.outputs['Y'], 1080.)       # widths are in 1080-pixel units
    sdf_node = b.new('CompositorNodeMaskToSDF')
    b.put(sdf_node.inputs['Mask'], b.math('GREATER_THAN', mask, .5))
    sdf = sdf_node.outputs['SDF']                              # pixels; negative inside
    # Every pixel reads the model's nearest edge pixel: is it lit, and in
    # what rim color.
    nearest = b.vector('ADD', sdf_node.outputs['Nearest Pixel'], (.5, .5, 0.))
    uv = b.vector('DIVIDE', nearest, info.outputs['Resolution'])
    samples = []
    for name in ('Mask', 'Rim color'):
        sample = b.new('CompositorNodeMapUV')
        b.put(sample.inputs['Image'], source.outputs[name])
        b.put(sample.inputs['UV'], uv)
        samples.append(sample.outputs['Image'])
    near = b.new('CompositorNodeSeparateColor')
    b.put(near.inputs['Image'], samples[0])
    held = b.math('MAXIMUM', near.outputs['Red'], 1e-4)
    lit = b.math('MINIMUM', b.math('DIVIDE', near.outputs['Green'], held), 1.)
    rim = b.vector('DIVIDE', samples[1], held)

    radius = b.mul(p['Size'], scale)
    outer = b.mul(radius, p['Position'])
    inner = b.mul(radius, b.one_minus(p['Position']))
    soft = b.add(.5, b.mul(p['Softness'], scale))
    # Lit side, shadow side or all round.
    side = b.mul(b.math('MULTIPLY_ADD', p['Lit only'], b.sub(lit, 1.), 1.),
                 b.math('MULTIPLY_ADD', p['Shadow only'], b.mul(lit, -1.), 1.))
    strength = b.mul(p['Opacity'], side)
    # Outside part: everything within the stroke, drawn behind the model.
    beyond = b.one_minus(b.smooth(sdf, b.sub(outer, soft), b.add(outer, soft)))
    behind = b.mul(b.mul(beyond, b.one_minus(mask)), strength)
    # Inside part: a band along the model's own edge, drawn over it.
    band = b.smooth(sdf, b.sub(b.mul(inner, -1.), soft), b.add(b.mul(inner, -1.), soft))
    over = b.mul(b.mul(b.mul(band, mask), b.math('GREATER_THAN', inner, 0.)), strength)
    color = b.mix(lit, color_node.outputs[0], b.vector('SCALE', rim, scale=p['Light']))

    split = b.new('CompositorNodeSeparateColor')
    b.put(split.inputs['Image'], image)
    rgb = b.new('CompositorNodeCombineColor')
    for channel in range(3):
        b.put(rgb.inputs[channel], split.outputs[channel])
    rgb.inputs[3].default_value = 1.
    result = layer(b, rgb.outputs[0], color, behind)
    result = layer(b, result, color, over)
    # Outer glow: rim-colored haze fading out past the stroke's lit side.
    glow_size = b.math('MAXIMUM', b.mul(p['Glow size'], scale), 1e-3)
    past = b.math('MAXIMUM', b.sub(sdf, outer), 0.)
    fade = b.one_minus(b.smooth(past, 0., glow_size))
    glow = b.mul(b.mul(b.mul(fade, fade), b.one_minus(mask)),
                 b.mul(b.mul(p['Glow strength'], lit), b.math('GREATER_THAN', p['Glow size'], 0.)))
    result = b.vector('ADD', result, b.vector('SCALE', rim, scale=glow))
    alpha = split.outputs['Alpha']
    alpha = b.add(b.mul(alpha, b.one_minus(behind)), behind)
    alpha = b.math('MAXIMUM', alpha, b.math('MINIMUM', glow, 1.))
    final = b.new('CompositorNodeSeparateColor')
    b.put(final.inputs['Image'], result)
    combined = b.new('CompositorNodeCombineColor')
    for channel in range(3):
        b.put(combined.inputs[channel], final.outputs[channel])
    b.put(combined.inputs[3], alpha)
    b.put(out.inputs['Image'], combined.outputs[0])
    tree[TAG] = VERSION
    return tree


def find_group(create=True):
    tree = next((g for g in bpy.data.node_groups if g.get(TAG) and g.bl_idname == 'CompositorNodeTree'), None)
    if tree is None:
        if not create:
            return None
        tree = bpy.data.node_groups.new(GROUP, 'CompositorNodeTree')
    if tree.get(TAG) != VERSION:
        build(tree)
    return tree


def ensure_passes(scene, enabled=True):
    for layer in scene.view_layers:
        names = {aov.name: aov for aov in layer.aovs}
        # Color AOVs only: in Blender 5.2 EEVEE a value AOV that comes first on
        # the view layer renders empty.
        for name, kind in ((STROKE_PASS, 'COLOR'), (STROKE_LIGHT_PASS, 'COLOR')):
            if enabled and name in names and names[name].type != kind:
                layer.aovs.remove(names.pop(name))
            if enabled and name not in names:
                aov = layer.aovs.add()
                aov.name, aov.type = name, kind
            elif not enabled and name in names:
                layer.aovs.remove(names[name])
        layer.update_render_passes()


def _output(tree):
    out = next((n for n in tree.nodes if n.type == 'GROUP_OUTPUT'), None)
    if out is None:
        return None, None
    return out, out.inputs.get('Image') or next((p for p in out.inputs if p.type == 'RGBA'), None)


def compositor(scene, create=True):
    tree = scene.compositing_node_group
    if tree is None and create:
        tree = bpy.data.node_groups.new('Sparkle Compositing', 'CompositorNodeTree')
        tree[OWNED] = True
        tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
        out = tree.nodes.new('NodeGroupOutput')
        render = tree.nodes.new('CompositorNodeRLayers')
        render.scene = scene
        tree.links.new(render.outputs['Image'], out.inputs['Image'])
        scene.compositing_node_group = tree
    return tree


def setup(scene):
    """Put the stroke last in the scene's compositor, before its output."""
    ensure_passes(scene)
    tree = compositor(scene)
    out, target = _output(tree)
    if target is None:
        return
    group = find_group()
    node = tree.nodes.get(NODE)
    if node is None or node.type != 'GROUP':
        source = target.links[0].from_socket if target.links else None
        if source is None:
            render = tree.nodes.new('CompositorNodeRLayers')
            render.scene = scene
            source = render.outputs['Image']
        node = tree.nodes.new('CompositorNodeGroup')
        node.name = node.label = NODE
        node.node_tree = group
        node.location = (out.location.x - 220, out.location.y)
        tree.links.new(source, node.inputs['Image'])
        tree.links.new(node.outputs['Image'], target)
    elif node.node_tree != group:
        node.node_tree = group
    passes = tree.nodes.get(PASSES)
    if passes is None or passes.type != 'R_LAYERS':
        passes = tree.nodes.new('CompositorNodeRLayers')
        passes.name = passes.label = PASSES
        passes.scene = scene
        passes.location = (node.location.x - 300, node.location.y - 260)
        layer = next((n for n in tree.nodes if n.type == 'R_LAYERS' and n != passes and (n.scene in (None, scene))), None)
        if layer is not None:
            passes.layer = layer.layer
    for port, name in (('Mask', STROKE_PASS), ('Rim color', STROKE_LIGHT_PASS)):
        output = passes.outputs.get(name)
        if output is not None and (not node.inputs[port].links or node.inputs[port].links[0].from_socket != output):
            tree.links.new(output, node.inputs[port])
    if PREVIOUS not in scene:
        scene[PREVIOUS] = scene.render.use_compositing
    scene.render.use_compositing = True
    update(scene)


def present(scene):
    """Whether the stroke is set up in the scene's compositor."""
    tree = scene.compositing_node_group
    return PREVIOUS in scene or (tree is not None and tree.nodes.get(NODE) is not None)


def remove(scene):
    tree = scene.compositing_node_group
    if tree is not None:
        node = tree.nodes.get(NODE)
        if node is not None:
            port = node.inputs['Image']
            source = port.links[0].from_socket if port.links else None
            for link in list(node.outputs['Image'].links):
                target = link.to_socket
                tree.links.remove(link)
                if source is not None:
                    tree.links.new(source, target)
            tree.nodes.remove(node)
        passes = tree.nodes.get(PASSES)
        if passes is not None:
            tree.nodes.remove(passes)
        if tree.get(OWNED) and not any(n.type not in ('GROUP_OUTPUT', 'R_LAYERS') for n in tree.nodes):
            scene.compositing_node_group = None
            if not tree.users:
                bpy.data.node_groups.remove(tree)
    ensure_passes(scene, enabled=False)
    if PREVIOUS in scene:
        scene.render.use_compositing = bool(scene[PREVIOUS])
        del scene[PREVIOUS]
    group = find_group(create=False)
    if group is not None and not group.users:
        bpy.data.node_groups.remove(group)


def update(scene, outlined=None):
    """Write the stroke's look. It is shared: the scene's toggle or any model's
    own stroke turns it on, and the models that take it are in its mask."""
    tree = find_group(create=False)
    if tree is None:
        return
    s = scene.mcbedrock_outlines
    wanted = stroke_wanted(scene, outlined)
    values = {
        'Size': s.stroke_size * s.line_size if wanted else 0.,
        'Position': POSITIONS[s.stroke_position],
        'Opacity': s.stroke_opacity if wanted else 0.,
        'Softness': s.stroke_softness,
        'Light': s.stroke_light,
        'Lit only': 1. if s.stroke_side == 'LIGHT' else 0.,
        'Shadow only': 1. if s.stroke_side == 'SHADOW' else 0.,
        'Glow size': s.stroke_glow if wanted else 0.,
        'Glow strength': s.stroke_glow_strength,
    }
    for mode in BLENDS:
        values['Blend ' + mode] = float(mode == s.stroke_blend)
    for name, item in values.items():
        node = tree.nodes.get(name)
        if node is not None:
            set_value(node.outputs[0], 'default_value', float(item))
    node = tree.nodes.get('Stroke color')
    if node is not None:
        set_value(node.outputs[0], 'default_value', (*tuple(s.stroke_color), 1.))
