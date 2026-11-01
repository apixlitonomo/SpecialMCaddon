"""Sparkle outlines: hand-drawn style lines, rims and bevels from edge data.

The look follows Minecraft key art and render artists' Photoshop finishes:
shadow-side silhouettes inked in a deeper shade of the surface's own hue, a
glowing rim on the side each light comes from, lighter and darker inner edges,
an optional light-catching bevel (Element Animation's edge highlights) and a
gradient across each face. outline_stroke.py adds a Photoshop-style Stroke
around models in the compositor.

Edges are worked out once from the geometry (outline_data.py) and stored on
the mesh as corner attributes: each pixel gets its exact distance to every
outline edge of its face. The shader turns those into screen-space widths,
measured from the shape's true outline (one face over for thin, edge-on
faces), so lines hug the silhouette at any angle. Rims take their color and
strength from up to six scene lights of any kind. There are no rays and no
screen pass, so the cost is a little math per pixel in EEVEE and Cycles alike
and stays flat for whole worlds. One shared node group carries every setting;
sliders only write its values.

Each model may also carry line settings of its own (Object.mcbedrock_outlines),
packed into a few custom properties that the group reads per object, so
models that share materials can still look different. Objects without them
use the scene's settings, as before. World blocks never read them: materials
only world blocks use get a copy of the group without that part.
"""
import math
import re

import bpy
from bpy.app.handlers import persistent

from . import outline_data
from ..exporters.miex.world_scale import subtree_objects

ATTR = 'sparkle_outline'              # FLOAT_VECTOR corner: edges 0..2
ATTR_LAST = 'sparkle_outline_3'       # FLOAT corner: edge 3
ATTR_DEPTH = 'sparkle_outline_depth'  # FLOAT corner: the box's thickness behind the face
MARK = 'mc_outline_data'              # mesh: edge data version written
OPT = 'mc_outlines'                   # object: explicit opt-out (False)
STROKE_FLAG = 'mc_stroke'             # object: drawn into the compositor stroke mask
WORLD_FLAG = 'mc_outline_world'       # object: world blocks, drawn with subtle lines
MODEL_FLAG = 'mc_outline_model'       # object: world block picked out for model lines on top of its world edges
WORLD = 'mc_world_outlines'           # MiEx world collection opted in
OWN = 'mc_outline_own'                # object: its own line settings, OWN_0 .. OWN_11 (see OWN_LAYOUT)
GROUP = 'Sparkle Outlines'            # materials on models: reads each model's own settings
WORLD_GROUP = 'Sparkle Outlines world'  # materials only world blocks use: the scene's settings alone
KIND = 'mc_outline_kind'              # node group: 'lines' (GROUP) or 'world' (WORLD_GROUP)
NODE = 'Sparkle Outlines'
GLOW = 'Sparkle Outline glow'
ADD = 'Sparkle Outline light'
NORMAL = 'Sparkle Outline normal'
PASS_NODE = 'Sparkle Outline pass'
STROKE_PASS, STROKE_LIGHT_PASS = 'Sparkle stroke', 'Sparkle stroke light'
LINE_PASS = 'Sparkle line masks'
TAG = 'mc_outline_graph'
VERSION = 31                          # 1..6 were the ray-traced outlines; 29 reads models' own settings; 30 lines on picked world blocks; 31 world copy holds world edges only
DATA_VERSION = 2                      # 2: model faces cut into strips measured whole
FAR = 1000.                           # offset marking convex edges with no box beyond
WORLD_BAND = 1 / 16                   # world edge width: one texture pixel of a block
LIGHTS = 6                            # the most lights that paint rims
MIN_LIGHTS = 2                        # the sky's sun and moon: crossing dusk never rebuilds the group
SHAPE = 'mc_outline_shape'            # models' group: (light slots, reads models' own settings)
LUMINANCE = (.2126, .7152, .0722)
# Thin cross-plane plants, liquids, glass and effects have no block outline.
SKIP = re.compile(r'water|glass|(?:^|[^a-z])ice|short_grass|tall_grass|seagrass|fern|flower|dandelion|poppy|'
                  r'tulip|orchid|allium|oxeye|cornflower|lily_of_the_valley|vine|sapling|bush|petal|'
                  r'lava|fire|portal|particle|cobweb|kelp|rail|torch|lantern_chain|chain')
# Leaves keep their edges, softened, as Element did for foliage.
SOFT = re.compile(r'leaves|leaf|azalea|moss|wart_block|mangrove_roots')

# Legacy ray-traced outlines (graph versions 1..6), removed on sight.
LEGACY_NODE, LEGACY_GLOW, LEGACY_ADD, LEGACY_AOV = 'MC Outlines', 'MC Outline glow', 'MC Outline light', 'MC Outline emission pass'
LEGACY_MARKER = 'mc_outline_version'

# name, default. Widths are in pixels of a 1080-pixel-high image.
PARAMETERS = (
    ('Pixel per depth', .001), ('Pixel constant', 0.), ('Pixel AA', 1.), ('Default thickness', 1.),
    ('Ink width', 3.), ('Ink opacity', .85), ('Ink shadow side', 1.), ('Ink custom', 0.), ('Ink depth', .8),
    ('Rim width', 7.), ('Rim opacity', 1.), ('Rim spread', .15), ('Rim custom', 1.), ('Rim glow', .35),
    ('Rim light color', 0.), ('Light falloff', .6), ('Light reference', 1.), ('Rim fade', 0.), ('Rim taper', .3), ('Rim gradient', 0.),
    ('Ink fade', 0.), ('Ink taper', .5), ('Ink gradient', 0.),
    ('Edge width', 2.), ('Edge highlight', .7), ('Edge shade', .5), ('Edge follow light', 1.),
    ('Bevel size', .06), ('Bevel strength', 0.), ('Bevel light', 0.),
    ('Gradient', .35), ('Weight variation', .3), ('Fade start', 1e4), ('Fade end', 4e4),
    ('Fade light sun', 1.), ('Fade near', 0.), ('Fade far', 1.),
    ('World lines', .5), ('World block pixels', 160.), ('World bevel', .5), ('World bevel light', .3),
    ('Debug masks', 0.), ('Stroke', 1.),
)
COLORS = (('Ink color', (.012, .008, .01)), ('Rim color', (.71, .65, .36)),
          ('Ink color 2', (.2, .1, .06)), ('Rim color 2', (.9, .55, .25)), ('Edge color', (1., 1., 1.)))
OWN_COLORS = frozenset(name for name, _ in COLORS)
# A model's own settings as the group reads them: twelve 4-float custom
# properties OWN_0 .. OWN_11 on the object, read by Object attributes (exact
# floats in EEVEE and Cycles). Colors take three places. 'Own' is 1; on an
# object without them every place reads 0, so the scene's values apply.
# '<layer> blend' is the index of the layer's mode in BLENDS[layer].
OWN_LAYOUT = (
    ('Own', 'Ink width', 'Ink opacity', 'Rim width'),
    ('Rim opacity', 'Edge width', 'Edge highlight', 'Edge shade'),
    ('Ink shadow side', 'Ink custom', 'Ink depth', 'Rim spread'),
    ('Rim custom', 'Rim light color', 'Rim glow', 'Rim fade'),
    ('Rim taper', 'Rim gradient', 'Light falloff', 'Ink fade'),
    ('Ink taper', 'Ink gradient', 'Edge follow light', 'Bevel size'),
    ('Bevel strength', 'Bevel light', 'Gradient', 'Weight variation'),
    ('Ink color', 'Highlight blend'),
    ('Rim color', 'Shade blend'),
    ('Ink color 2', 'Ink blend'),
    ('Rim color 2', 'Rim blend'),
    ('Edge color', 'Stroke'),
)
assert all(sum(3 if name in OWN_COLORS else 1 for name in names) == 4 for names in OWN_LAYOUT)
# Settings that stay the scene's while a model is edited: world blocks,
# distance, the masks view and the stroke's look (one compositor pass). Every
# other setting is a line setting a model can have its own of (LINE_SETTINGS).
SCENE_ONLY = frozenset(('world_lines', 'fade_distance', 'show_masks', 'stroke_side', 'stroke_position', 'stroke_size',
                        'stroke_blend', 'stroke_opacity', 'stroke_color', 'stroke_light', 'stroke_softness',
                        'stroke_glow', 'stroke_glow_strength'))
KEY = 'Key light direction'
FADE_LIGHT = 'Fade light'          # toward a sun, or a lamp's position: where Rim/Ink fade falls off from
# Photoshop-style blending per layer. Every mode is evaluated and one-hot
# weights pick one, so switching modes rewrites values instead of shaders.
BLENDS = {
    'Highlight': ('MIX', 'SCREEN', 'OVERLAY', 'SOFT_LIGHT', 'ADD'),
    'Shade': ('MIX', 'MULTIPLY', 'OVERLAY', 'SOFT_LIGHT'),
    'Ink': ('MIX', 'MULTIPLY', 'OVERLAY', 'SOFT_LIGHT'),
    'Rim': ('MIX', 'SCREEN', 'OVERLAY', 'SOFT_LIGHT', 'ADD'),
}
BLEND_LABELS = {'MIX': 'Normal', 'SCREEN': 'Screen', 'OVERLAY': 'Overlay', 'SOFT_LIGHT': 'Soft Light',
                'ADD': 'Add', 'MULTIPLY': 'Multiply'}
BLEND_FIELDS = (('Highlight', 'edge_highlight_blend'), ('Shade', 'edge_shade_blend'),
                ('Ink', 'ink_blend'), ('Rim', 'rim_blend'))


def blend_items(layer):
    return tuple((mode, BLEND_LABELS[mode], BLEND_LABELS[mode] + ' blend') for mode in BLENDS[layer])


def light_names(index):
    prefix = 'Light %d ' % (index + 1)
    return {key: prefix + key for key in ('position', 'axis', 'color', 'sun', 'cone', 'cone blend', 'area')}


# ---------------------------------------------------------------- graph

class _Build:
    """Small shader-graph writer; every helper returns an output socket."""

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

    def rgb(self, name, default):
        node = self.new('ShaderNodeRGB', name)
        node.outputs[0].default_value = (*default[:3], 1.)
        return node.outputs[0]

    def xyz(self, name, default):
        node = self.new('ShaderNodeCombineXYZ', name)
        for socket, item in zip(node.inputs, default):
            socket.default_value = item
        return node.outputs[0]

    def math(self, operation, a, b=0., c=0.):
        node = self.new('ShaderNodeMath', operation=operation)
        for socket, value in zip(node.inputs, (a, b, c)):
            self.put(socket, value)
        return node.outputs[0]

    def add(self, a, b): return self.math('ADD', a, b)
    def sub(self, a, b): return self.math('SUBTRACT', a, b)
    def mul(self, a, b): return self.math('MULTIPLY', a, b)
    def madd(self, a, b, c): return self.math('MULTIPLY_ADD', a, b, c)
    def one_minus(self, a): return self.math('SUBTRACT', 1., a)

    def most(self, items, operation='MAXIMUM'):
        result = items[0]
        for item in items[1:]:
            result = self.math(operation, result, item)
        return result

    def vector(self, operation, a, b=None, scale=None):
        node = self.new('ShaderNodeVectorMath', operation=operation)
        self.put(node.inputs[0], a)
        if b is not None:
            self.put(node.inputs[1], b)
        if scale is not None:
            self.put(node.inputs['Scale'], scale)
        return node.outputs['Value' if operation in ('DOT_PRODUCT', 'LENGTH', 'DISTANCE') else 'Vector']

    def dot(self, a, b): return self.vector('DOT_PRODUCT', a, b)
    def scaled(self, a, s): return self.vector('SCALE', a, scale=s)
    def vadd(self, a, b): return self.vector('ADD', a, b)

    def smooth(self, value, low, high):
        node = self.new('ShaderNodeMapRange', interpolation_type='SMOOTHSTEP', clamp=True)
        for name, item in (('Value', value), ('From Min', low), ('From Max', high)):
            self.put(node.inputs[name], item)
        return node.outputs['Result']

    def gamma(self, color, power):
        node = self.new('ShaderNodeGamma')
        self.put(node.inputs['Color'], color)
        node.inputs['Gamma'].default_value = power
        return node.outputs[0]

    def layer(self, name, base, paint, amount, own=None, chosen=None):
        """Paint over base with Photoshop's blend modes, weighted by '<name> blend <mode>'
        (or, where own is 1, by the model's own chosen mode index).

        Photoshop blends in display (gamma-encoded) values. Blending linear
        values instead makes Soft Light and Overlay saturate and burn warm
        colors, so both layers are encoded, blended and decoded again.
        """
        base_display, paint_display = self.gamma(base, 1 / 2.2), self.gamma(paint, 1 / 2.2)
        result = None
        for index, mode in enumerate(BLENDS[name]):
            node = self.new('ShaderNodeMix', data_type='RGBA', blend_type=mode, clamp_result=mode != 'ADD')
            self.put(node.inputs[0], amount)
            self.put(node.inputs[6], base_display)
            self.put(node.inputs[7], paint_display)
            weight = self.value('%s blend %s' % (name, mode), 1. if index == 0 else 0.)
            if own is not None:
                weight = self.mix(own, weight, self.math('COMPARE', chosen, float(index), .5), 'FLOAT')
            part = self.vector('SCALE', node.outputs[2], scale=weight)
            result = part if result is None else self.vector('ADD', result, part)
        return self.gamma(result, 2.2)

    def own(self):
        """A model's own line settings, {name: socket}, read per object (OWN_LAYOUT).

        Colors are the x, y and z of their property and the value after them
        its fourth place (Alpha). An object without them reads zeros: Own 0."""
        found = {}
        for index, names in enumerate(OWN_LAYOUT):
            attribute = self.new('ShaderNodeAttribute', 'Own settings %d' % (index + 1),
                                 attribute_type='OBJECT', attribute_name='%s_%d' % (OWN, index))
            split = self.new('ShaderNodeSeparateXYZ')
            self.put(split.inputs[0], attribute.outputs['Vector'])
            place = 0
            for name in names:
                if name in OWN_COLORS:
                    found[name] = attribute.outputs['Color']
                    place += 3
                else:
                    found[name] = split.outputs[place] if place < 3 else attribute.outputs['Alpha']
                    place += 1
        return found

    def mix(self, factor, a, b, kind='RGBA'):
        node = self.new('ShaderNodeMix', data_type=kind, clamp_result=False)
        self.put(node.inputs[0], factor)
        first, result = {'FLOAT': (2, 0), 'VECTOR': (4, 1), 'RGBA': (6, 2)}[kind]
        self.put(node.inputs[first], a)
        self.put(node.inputs[first + 1], b)
        return node.outputs[result]

    def edge_data(self, boxes=True):
        """The corner attributes decoded per edge: (valid, distance in world
        units, open, boxed) lists of four (boxed empty unless boxes), and the
        object-to-world scale.

        Per corner, distance + 1 to each outline edge (negative for open
        edges, + FAR for convex edges with no box beyond, 0 for none)."""
        edges = self.new('ShaderNodeAttribute', 'Outline edges', attribute_type='GEOMETRY', attribute_name=ATTR)
        last = self.new('ShaderNodeAttribute', 'Outline edge 4', attribute_type='GEOMETRY', attribute_name=ATTR_LAST)
        split = self.new('ShaderNodeSeparateXYZ')
        self.put(split.inputs[0], edges.outputs['Vector'])
        raw = [split.outputs[0], split.outputs[1], split.outputs[2], last.outputs['Fac']]
        to_world = self.new('ShaderNodeVectorTransform', vector_type='VECTOR', convert_from='OBJECT', convert_to='WORLD')
        to_world.inputs[0].default_value = (.57735, .57735, .57735)
        scale = self.vector('LENGTH', to_world.outputs[0])
        valid, dist, opened, boxed = [], [], [], []
        for v in raw:
            size = self.math('ABSOLUTE', v)
            ok = self.math('GREATER_THAN', size, .5)
            far = self.math('GREATER_THAN', size, FAR * .5)
            flat = self.math('MAXIMUM', self.sub(self.sub(size, 1.), self.mul(far, FAR)), 0.)
            is_open = self.math('LESS_THAN', v, -.5)
            valid.append(ok)
            dist.append(self.mul(self.mul(flat, ok), scale))
            opened.append(is_open)
            if boxes:
                boxed.append(self.mul(self.mul(ok, self.one_minus(far)), self.one_minus(is_open)))
        return valid, dist, opened, boxed, scale

    def tangent(self, height, normal):
        """The face direction a distance field falls toward: the bump node
        differentiates it on the deformed surface, its tilt points down the
        slope, toward the edge."""
        bump = self.new('ShaderNodeBump')
        bump.inputs['Strength'].default_value = 1.
        bump.inputs['Distance'].default_value = 1.
        self.put(bump.inputs['Height'], height)
        self.put(bump.inputs['Normal'], normal)
        tilted = bump.outputs['Normal']
        side = self.vector('SUBTRACT', tilted, self.scaled(normal, self.dot(tilted, normal)))
        return self.scaled(self.vector('NORMALIZE', side), -1.)

    def face_shade(self, dist, valid, TsL, TtL, gradient, bevel_mask, bevel_light):
        """Face gradient: lighter toward the corner that faces the key light,
        on faces with outline edges on both sides (cubes, not a field's
        middle), times the bevel's light where edges catch it. TsL and TtL
        are the face directions' dot products with the key light."""
        both_s, both_t = self.mul(valid[1], valid[3]), self.mul(valid[0], valid[2])
        s = self.math('DIVIDE', dist[3], self.math('MAXIMUM', self.add(dist[3], dist[1]), 1e-6))
        t = self.math('DIVIDE', dist[0], self.math('MAXIMUM', self.add(dist[0], dist[2]), 1e-6))
        u = self.add(self.mul(self.mul(self.sub(s, .5), TsL), both_s), self.mul(self.mul(self.sub(t, .5), TtL), both_t))
        shade = self.math('MAXIMUM', self.madd(self.mul(u, 2.), gradient, 1.), 0.)
        return self.mul(shade, self.madd(bevel_mask, bevel_light, 1.))


def interface(tree):
    for name, direction, kind, default in (
            ('Color', 'INPUT', 'NodeSocketColor', (.8, .8, .8, 1)),
            ('Alpha', 'INPUT', 'NodeSocketFloat', 1.),
            ('Normal', 'INPUT', 'NodeSocketVector', None),
            ('Strength', 'INPUT', 'NodeSocketFloat', 1.),
            ('Color', 'OUTPUT', 'NodeSocketColor', None),
            ('Emission', 'OUTPUT', 'NodeSocketColor', None),
            ('Normal', 'OUTPUT', 'NodeSocketVector', None),
            ('Line', 'OUTPUT', 'NodeSocketFloat', None),
            ('Stroke mask', 'OUTPUT', 'NodeSocketColor', None),
            ('Stroke light', 'OUTPUT', 'NodeSocketColor', None),
            ('Line masks', 'OUTPUT', 'NodeSocketColor', None)):
        socket = tree.interface.new_socket(name=name, in_out=direction, socket_type=kind)
        if default is not None:
            socket.default_value = default


def build(tree):
    """Write the outline graph into tree, replacing previous contents."""
    tree.nodes.clear()
    sockets = {item.name: item.socket_type for item in tree.interface.items_tree
               if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT'}
    if sockets.get('Stroke mask') != 'NodeSocketColor' or sockets.get('Line masks') != 'NodeSocketColor':
        tree.interface.clear()
        interface(tree)
    b = _Build(tree)
    source, out = b.new('NodeGroupInput'), b.new('NodeGroupOutput')
    if tree.get(KIND) == 'world':
        build_world(b, source, out)
        tree[TAG] = VERSION
        return tree
    p = {name: b.value(name, default) for name, default in PARAMETERS}
    for name, default in COLORS:
        p[name] = b.rgb(name, default)
    slots, reads_own = shape(tree)
    # Models with line settings of their own (materials are often shared, so
    # they are read per object): where an object has none, Own is 0 and every
    # value stays the scene's, exactly. Until some model has settings of its
    # own the group skips the lookups (reshape adds them), and the world copy
    # (build_world) never has them: EEVEE looks up every Object attribute on
    # every object of a material at each redraw, and on every pixel the twelve
    # lookups were a third of what lines cost a character close-up.
    mine = b.own() if reads_own else {}
    own = mine.get('Own')
    for name, socket in mine.items():
        if name in p:
            p[name] = b.mix(own, p[name], socket, 'RGBA' if name in OWN_COLORS else 'FLOAT')
    modes = {layer: (own, mine.get(layer + ' blend')) for layer in BLENDS}
    key = b.vector('NORMALIZE', b.xyz(KEY, (.35, -.45, .82)))
    strength = source.outputs['Strength']

    valid, dist, opened, boxed, scale = b.edge_data()
    depth = b.new('ShaderNodeAttribute', 'Outline thickness', attribute_type='GEOMETRY', attribute_name=ATTR_DEPTH)
    thick = b.mix(b.math('GREATER_THAN', depth.outputs['Fac'], 0.), p['Default thickness'],
                  b.mul(depth.outputs['Fac'], scale), 'FLOAT')

    geometry = b.new('ShaderNodeNewGeometry')
    # The true face normal throughout: smoothed or custom normals would bend
    # the edge directions and gradients across flat block faces.
    N, I, P = geometry.outputs['True Normal'], geometry.outputs['Incoming'], geometry.outputs['Position']
    Ts = b.tangent(b.sub(dist[3], dist[1]), N)   # from edge 3 toward edge 1
    Tt = b.tangent(b.sub(dist[0], dist[2]), N)   # from edge 0 toward edge 2
    outward = (b.scaled(Tt, -1.), Ts, Tt, b.scaled(Ts, -1.))
    camera = b.new('ShaderNodeCameraData')
    z = camera.outputs['View Z Depth']
    pixel = b.math('MAXIMUM', b.madd(z, p['Pixel per depth'], p['Pixel constant']), 1e-8)
    # World blocks get subtle edges that keep to a sliver of each block: full
    # World lines strength while a block covers World block pixels on screen,
    # thinner as blocks get smaller with distance. Models keep their widths.
    world = b.new('ShaderNodeAttribute', 'World flag', attribute_type='OBJECT', attribute_name=WORLD_FLAG)
    # One block is one world unit (MiEx meshes carry a 1/16 object scale).
    block_px = b.math('DIVIDE', 1., pixel)
    world_scale = b.mul(p['World lines'], b.math('MINIMUM', b.math('DIVIDE', block_px, p['World block pixels']), 1.))
    world_flag = b.math('MINIMUM', world.outputs['Fac'], 1.)
    # World blocks picked out (trees) keep their world edges and also draw
    # model lines at full width.
    picked_out = b.new('ShaderNodeAttribute', 'Model flag', attribute_type='OBJECT', attribute_name=MODEL_FLAG)
    picked_flag = b.math('MINIMUM', picked_out.outputs['Fac'], 1.)
    world_only = b.mul(world_flag, b.one_minus(picked_flag))
    line_scale = b.math('MAXIMUM', b.mix(world_only, 1., world_scale, 'FLOAT'), 1e-4)
    screen_pixel = pixel
    # Distances divided by a smaller pixel come out larger: thinner lines.
    pixel = b.mul(pixel, line_scale)
    NI = b.math('ABSOLUTE', b.dot(N, I))
    NL = b.dot(N, key)
    TsI, TtI, TsL, TtL = b.dot(Ts, I), b.dot(Tt, I), b.dot(Ts, key), b.dot(Tt, key)

    def across(along_i):
        """How much of a band beside an edge survives projection: 1 head-on."""
        return b.math('MAXIMUM', b.math('SQRT', b.math('MAXIMUM', b.one_minus(b.mul(along_i, along_i)), 0.)), 1e-3)

    across_s, across_t = across(TsI), across(TtI)
    # Edge k: O.I, O.key and its band's projection. Edges 0/2 run along Ts,
    # edges 1/3 along Tt.
    frame = ((b.mul(TtI, -1.), b.mul(TtL, -1.), across_s), (TsI, TsL, across_t),
             (TtI, TtL, across_s), (b.mul(TsI, -1.), b.mul(TsL, -1.), across_t))
    aa = b.mul(p['Pixel AA'], .5)

    def cover(px, width):
        return b.one_minus(b.smooth(px, b.sub(width, aa), b.add(width, aa)))

    minus_n = b.scaled(N, -1.)
    half_pixel = b.mul(screen_pixel, .5)
    to_outline, hidden, lights, darks, bevels = [], [], [], [], []
    for k in range(4):
        oi, ol, across_k = frame[k]
        here = b.math('DIVIDE', NI, across_k)
        beside = b.math('DIVIDE', b.math('MAXIMUM', oi, 0.), across_k)
        visible = b.mul(b.smooth(oi, 0., .03), b.one_minus(opened[k]))
        # Distance on screen to the shape's outline past this edge: the edge
        # itself when the neighbour turns away, the neighbour's far side when
        # it is a thin box face seen edge-on, nowhere for an open fold.
        beyond = b.add(b.mul(b.mul(thick, beside), boxed[k]), b.mul(b.mul(visible, b.one_minus(boxed[k])), 1e6))
        on_screen = b.mul(dist[k], here)
        outline_px = b.math('DIVIDE', b.add(on_screen, beyond), pixel)
        to_outline.append(b.add(outline_px, b.mul(b.one_minus(valid[k]), 1e6)))
        # The face hidden behind the outline: the neighbour itself, or for a
        # box seen past an edge-on face, the side opposite this one.
        hidden.append(b.mix(b.mul(visible, boxed[k]), outward[k], minus_n, 'VECTOR'))
        # Inner edges: a lighter line where a bevel there would catch the key
        # light, a darker one where it would turn away. An edge within a few
        # pixels of the outline (a box side seen almost edge-on, or the
        # outline itself) leaves the line to the rim: both drew two light
        # lines side by side.
        edge_px = b.math('DIVIDE', on_screen, pixel)
        gap = b.math('DIVIDE', beyond, pixel)
        apart = b.smooth(gap, b.add(p['Rim width'], 1.), b.add(p['Rim width'], 4.))
        inner = b.mul(b.mul(cover(edge_px, p['Edge width']), b.mul(visible, valid[k])), apart)
        toward = b.smooth(b.mul(b.add(NL, ol), .7071), -.05, .35)
        # Follow light 0: every edge gets the light line, like hand-painted
        # edge lights; 1: only edges that face the light.
        toward = b.madd(p['Edge follow light'], b.sub(toward, 1.), 1.)
        lights.append(b.mul(inner, toward))
        darks.append(b.mul(inner, b.one_minus(toward)))
        soft_bevel = b.one_minus(b.smooth(dist[k], 0., p['Bevel size']))
        # World blocks: a hard band one texture pixel wide, antialiased to the
        # screen pixel, so the edge sits on the block like Element's corner
        # atlas instead of a soft gradient beside it.
        texel_bevel = b.one_minus(b.smooth(dist[k], b.sub(WORLD_BAND, half_pixel), b.add(WORLD_BAND, half_pixel)))
        bevels.append(b.mul(b.mix(world_flag, soft_bevel, texel_bevel, 'FLOAT'),
                            b.mul(valid[k], b.one_minus(opened[k]))))

    # The nearest outline wins; its hidden face decides the rim's light.
    nearest = b.most(to_outline, 'MINIMUM')
    picked = [b.math('LESS_THAN', item, b.add(nearest, .01)) for item in to_outline]
    H = hidden[0]
    H = b.scaled(H, picked[0])
    for k in range(1, 4):
        H = b.vadd(H, b.scaled(hidden[k], picked[k]))
    H = b.vector('NORMALIZE', H)

    # Every light paints the rims that face it, in its own color: suns by
    # direction, point, spot and area lights by position, falloff and cone.
    # One slot per light the scene has (reshape), an unused slot adding zero.
    rim_light, rim_amount, total = (0., 0., 0.), 0., 0.
    for index in range(slots):
        n = light_names(index)
        position = b.xyz(n['position'], (0., 0., 1.))
        axis = b.xyz(n['axis'], (0., 0., -1.))
        color = b.rgb(n['color'], (0., 0., 0.))
        sun = b.value(n['sun'], 1.)
        cone, blend = b.value(n['cone'], -2.), b.value(n['cone blend'], -1.5)
        area = b.value(n['area'], 0.)
        offset = b.vector('SUBTRACT', position, P)
        distance2 = b.math('MAXIMUM', b.dot(offset, offset), 1e-4)
        toward_light = b.mix(sun, b.vector('NORMALIZE', offset), position, 'VECTOR')
        falloff = b.mix(sun, b.math('DIVIDE', 1., distance2), 1., 'FLOAT')
        facing = b.mul(b.dot(toward_light, axis), -1.)
        spot = b.smooth(facing, cone, blend)
        emit = b.mix(area, 1., b.math('MAXIMUM', facing, 0.), 'FLOAT')
        weight = b.mul(b.mul(falloff, spot), emit)
        brightness = b.mul(b.dot(color, LUMINANCE), weight)
        reach = b.math('MAXIMUM', b.dot(H, toward_light), 0.)
        painted = b.scaled(color, b.mul(weight, reach))
        rim_light = painted if rim_light is None else b.vadd(rim_light, painted)
        amount = b.mul(brightness, reach)
        rim_amount = amount if rim_amount is None else b.add(rim_amount, amount)
        total = brightness if total is None else b.add(total, brightness)
    share = b.math('DIVIDE', rim_amount, b.math('MAXIMUM', total, 1e-6))
    lit = b.smooth(b.add(share, p['Rim spread']), .05, .35)
    # Falloff: rims thin and fade with each light's real falloff, compared
    # with how strongly the brightest light reaches the model. A sun has none.
    reached = b.smooth(b.math('DIVIDE', rim_amount, b.math('MAXIMUM', p['Light reference'], 1e-6)), 0., .6)
    lit = b.mul(lit, b.madd(p['Light falloff'], b.sub(reached, 1.), 1.))
    shadowed = b.one_minus(b.mul(lit, p['Ink shadow side']))
    weight = b.madd(p['Weight variation'], b.sub(.5, lit), 1.)

    def band(width, side, taper):
        """A line along the outline: as wide as width where its side holds,
        thinning toward its ends by taper. Returns (coverage, 0..1 across it)."""
        reach = b.mul(width, b.madd(taper, b.sub(side, 1.), 1.))
        across_band = b.math('MINIMUM', b.math('DIVIDE', nearest, b.math('MAXIMUM', reach, 1e-4)), 1.)
        return b.mul(cover(nearest, reach), side), across_band

    rim_band, rim_across = band(p['Rim width'], lit, p['Rim taper'])
    ink_band, ink_across = band(b.mul(p['Ink width'], weight), shadowed, p['Ink taper'])
    outline = b.math('MAXIMUM', rim_band, ink_band)

    # Fade: lines fall off with distance from the key light, the way light
    # does, in one sweep over every outlined model: full where the models are
    # nearest the light, gone (at 100%) where they are farthest. Distance runs
    # along a sun's direction, or out from a lamp. Ink, on the shadow side,
    # fades the other way. update() writes the models' nearest and farthest.
    fade_light = b.xyz(FADE_LIGHT, (0., 0., 1.))
    from_light = b.mix(p['Fade light sun'], b.vector('LENGTH', b.vector('SUBTRACT', P, fade_light)),
                       b.mul(b.dot(P, fade_light), -1.), 'FLOAT')
    span = b.math('MAXIMUM', b.sub(p['Fade far'], p['Fade near']), 1e-4)
    away = b.math('MINIMUM', b.math('MAXIMUM', b.math('DIVIDE', b.sub(from_light, p['Fade near']), span), 0.), 1.)
    # Falls off from the light at once, as light does, not after an easing
    # start: rims sit on the lit, near half, where a smoothstep kept them whole.
    left = b.one_minus(away)
    rim_kept = b.one_minus(b.mul(p['Rim fade'], b.one_minus(b.mul(left, left))))
    ink_kept = b.one_minus(b.mul(p['Ink fade'], b.one_minus(b.mul(away, away))))

    near = b.mul(b.one_minus(b.smooth(z, p['Fade start'], p['Fade end'])), strength)
    # World blocks are not outlined: like Element's worlds they only get the
    # soft inner edges. Rim and ink lines belong to models, rigs and world
    # blocks picked out for them.
    model = b.math('MAXIMUM', b.one_minus(world_flag), picked_flag)
    rim_band, ink_band = b.mul(rim_band, model), b.mul(ink_band, model)
    outline = b.mul(outline, model)
    rim = b.mul(b.mul(b.mul(rim_band, rim_kept), p['Rim opacity']), near)
    ink = b.mul(b.mul(b.mul(ink_band, ink_kept), p['Ink opacity']), near)
    inner_fade = b.mul(near, b.one_minus(outline))
    # World blocks get no drawn edge lines; their edges are lit (below).
    light_edge = b.mul(b.mul(b.mul(b.most(lights), p['Edge highlight']), inner_fade), model)
    dark_edge = b.mul(b.mul(b.mul(b.most(darks), p['Edge shade']), inner_fade), model)

    # Bevel: tilt the shading normal toward each nearby outline edge, so edges
    # catch real light (and shadow) in both engines. Sub-pixel bevels fade.
    resolved = b.smooth(b.math('DIVIDE', p['Bevel size'], screen_pixel), .5, 1.5)
    # World blocks always get Element's edges (bramstout.nl, Minecraft Live
    # 2024/2025): the albedo brightens at each block edge and the normal bends
    # along the distance field's gradient so the edge catches the light.
    amount = b.mul(b.mul(b.mix(world_flag, p['Bevel strength'], p['World bevel'], 'FLOAT'), near), resolved)
    tilt = b.vadd(b.scaled(Ts, b.sub(bevels[1], bevels[3])), b.scaled(Tt, b.sub(bevels[2], bevels[0])))
    bent = b.vector('NORMALIZE', b.vadd(source.outputs['Normal'], b.scaled(tilt, amount)))
    bevel_mask = b.mul(b.most(bevels), amount)

    shade = b.face_shade(dist, valid, TsL, TtL, b.mul(p['Gradient'], strength), bevel_mask,
                         b.mix(world_flag, p['Bevel light'], p['World bevel light'], 'FLOAT'))

    surface = source.outputs['Color']
    base = b.scaled(surface, shade)
    # Ink: a deeper, richer shade of the surface's own hue (each channel
    # scaled by itself, so saturated colors stay saturated), or a custom ink.
    deep = b.vector('MULTIPLY', surface, b.vadd(b.scaled(surface, .3), (.3, .3, .3)))
    deep = b.mix(p['Ink depth'], surface, deep)
    ink_color = b.mix(p['Ink custom'], deep, p['Ink color'])
    ink_color = b.mix(b.mul(ink_across, p['Ink gradient']), ink_color, p['Ink color 2'])
    lighter = b.vector('MINIMUM', b.vadd(b.scaled(surface, 1.45), (.07, .06, .04)), (1., 1., 1.))
    darker = b.vector('DIVIDE', surface, b.vector('MAXIMUM', b.vector('SUBTRACT', (2., 2., 2.), surface), (1., 1., 1.)))
    rim_color = b.mix(p['Rim custom'], lighter, p['Rim color'])
    # A rim in the lights' own colors: their hue at the painted brightness.
    hue = b.vector('DIVIDE', rim_light, b.math('MAXIMUM', b.dot(rim_light, (1., 1., 1.)), 1e-6))
    rim_color = b.mix(p['Rim light color'], rim_color, b.vector('MINIMUM', b.scaled(hue, b.mul(b.dot(rim_color, (1., 1., 1.)), 1.)), (1., 1., 1.)))
    rim_color = b.mix(b.mul(rim_across, p['Rim gradient']), rim_color, p['Rim color 2'])
    # Edge lights: painted with Edge color (white by default) through the
    # chosen blend, Soft Light by default, as artists paint them.
    color = b.layer('Highlight', base, p['Edge color'], light_edge, *modes['Highlight'])
    color = b.layer('Shade', color, darker, dark_edge, *modes['Shade'])
    color = b.layer('Ink', color, ink_color, ink, *modes['Ink'])
    color = b.layer('Rim', color, rim_color, rim, *modes['Rim'])
    masks = b.new('ShaderNodeCombineXYZ', 'Outline masks')
    b.put(masks.inputs[0], rim)
    b.put(masks.inputs[1], b.math('MAXIMUM', light_edge, dark_edge))
    b.put(masks.inputs[2], ink)
    color = b.mix(p['Debug masks'], color, masks.outputs[0])
    alpha = source.outputs['Alpha']
    # The glow carries the light that reaches the rim: brightest where a
    # light shines straight at it, in that light's color.
    glow = b.mul(b.mul(rim, p['Rim glow']), b.mul(alpha, b.one_minus(p['Debug masks'])))
    b.put(out.inputs['Color'], color)
    b.put(out.inputs['Emission'], b.scaled(b.vector('MULTIPLY', rim_color, rim_light), glow))
    b.put(out.inputs['Normal'], bent)
    b.put(out.inputs['Line'], b.most([rim, ink, light_edge, dark_edge]))
    # Photoshop masks (render_masks.py): rim, edge lights and ink, R G B.
    line_masks = b.new('ShaderNodeCombineXYZ', 'Line masks')
    # Full line shapes, not their opacity: masks select, paint decides.
    b.put(line_masks.inputs[0], b.mul(rim_band, alpha))
    b.put(line_masks.inputs[1], b.mul(b.math('MINIMUM', b.most(lights), 1.), b.mul(alpha, model)))
    b.put(line_masks.inputs[2], b.mul(ink_band, alpha))
    b.put(out.inputs['Line masks'], line_masks.outputs[0])
    # Compositor stroke passes: (coverage, coverage on the lit side) and the
    # rim's painted color, so the stroke's lit side matches the rim.
    flag = b.new('ShaderNodeAttribute', 'Stroke flag', attribute_type='OBJECT', attribute_name=STROKE_FLAG)
    # Stroke: whether this model takes the stroke (the scene's toggle, or its own).
    stroked = b.mul(b.mul(alpha, b.math('MINIMUM', flag.outputs['Fac'], 1.)), p['Stroke'])
    stroke_mask = b.new('ShaderNodeCombineXYZ', 'Stroke coverage')
    b.put(stroke_mask.inputs[0], stroked)
    b.put(stroke_mask.inputs[1], b.mul(stroked, lit))
    b.put(out.inputs['Stroke mask'], stroke_mask.outputs[0])
    b.put(out.inputs['Stroke light'], b.scaled(rim_color, stroked))
    tree[TAG] = VERSION
    return tree


# The values the world copy reads; update() writes the others into the models' group only.
WORLD_PARAMETERS = frozenset(('Pixel per depth', 'Pixel constant', 'Bevel size', 'Gradient', 'Fade start',
                              'Fade end', 'World bevel', 'World bevel light', 'Debug masks'))


def build_world(b, source, out):
    """The group for materials only world blocks use (sync): Element's world
    edges and the face gradient, nothing else.

    World blocks draw no lines, so in the full graph every rim, ink and edge
    line term is multiplied by zero for them, and the four blend layers, with
    nothing to paint, only clamp the color to 0..1. This copy computes the
    rest the same way in about a ninth of the nodes: an imported world's
    hundreds of materials compile, set up for each render and shade that much
    faster, with the same picture."""
    p = {name: b.value(name, default) for name, default in PARAMETERS if name in WORLD_PARAMETERS}
    key = b.vector('NORMALIZE', b.xyz(KEY, (.35, -.45, .82)))
    strength = source.outputs['Strength']
    valid, dist, opened, _boxed, _scale = b.edge_data(boxes=False)
    N = b.new('ShaderNodeNewGeometry').outputs['True Normal']
    Ts = b.tangent(b.sub(dist[3], dist[1]), N)
    Tt = b.tangent(b.sub(dist[0], dist[2]), N)
    z = b.new('ShaderNodeCameraData').outputs['View Z Depth']
    pixel = b.math('MAXIMUM', b.madd(z, p['Pixel per depth'], p['Pixel constant']), 1e-8)
    half_pixel = b.mul(pixel, .5)
    # A hard band one texture pixel wide, antialiased to the screen pixel, so
    # the edge sits on the block like Element's corner atlas.
    bevels = [b.mul(b.one_minus(b.smooth(dist[k], b.sub(WORLD_BAND, half_pixel), b.add(WORLD_BAND, half_pixel))),
                    b.mul(valid[k], b.one_minus(opened[k]))) for k in range(4)]
    near = b.mul(b.one_minus(b.smooth(z, p['Fade start'], p['Fade end'])), strength)
    resolved = b.smooth(b.math('DIVIDE', p['Bevel size'], pixel), .5, 1.5)
    amount = b.mul(b.mul(p['World bevel'], near), resolved)
    tilt = b.vadd(b.scaled(Ts, b.sub(bevels[1], bevels[3])), b.scaled(Tt, b.sub(bevels[2], bevels[0])))
    bent = b.vector('NORMALIZE', b.vadd(source.outputs['Normal'], b.scaled(tilt, amount)))
    bevel_mask = b.mul(b.most(bevels), amount)
    shade = b.face_shade(dist, valid, b.dot(Ts, key), b.dot(Tt, key), b.mul(p['Gradient'], strength),
                         bevel_mask, p['World bevel light'])
    base = b.scaled(source.outputs['Color'], shade)
    # What the empty line layers leave: Photoshop's clamp of the color to 0..1.
    clamp = b.new('ShaderNodeMix', data_type='RGBA', blend_type='MIX', clamp_result=True)
    clamp.inputs[0].default_value = 0.
    b.put(clamp.inputs[6], base)
    b.put(clamp.inputs[7], base)
    # Show line masks: world blocks have none, so they show black.
    b.put(out.inputs['Color'], b.mix(p['Debug masks'], clamp.outputs[2], (0., 0., 0., 1.)))
    b.put(out.inputs['Normal'], bent)
    # No rim glow, lines, stroke or line masks: zeros, linked so that nothing
    # downstream falls back to a socket's own default (white emission).
    black, zero = b.rgb('No lines', (0., 0., 0.)), b.value('No line', 0.)
    for name in ('Emission', 'Stroke mask', 'Stroke light', 'Line masks'):
        b.put(out.inputs[name], black)
    b.put(out.inputs['Line'], zero)


def find_group(create=True, world=False):
    """The shared group for models (world=False) or for materials only world blocks use."""
    kind = 'world' if world else 'lines'
    tree = next((g for g in bpy.data.node_groups if g.get(TAG) and g.get(KIND) == kind), None)
    if tree is None:
        if not create:
            return None
        tree = bpy.data.node_groups.new(WORLD_GROUP if world else GROUP, 'ShaderNodeTree')
        tree[KIND] = kind
        if not world:
            tree[SHAPE] = (MIN_LIGHTS, 0)
    if tree.get(TAG) != VERSION:
        build(tree)
    return tree


def shape(tree):
    """(light slots, reads models' own settings) the models' group is built
    with. Groups from before carry everything: six slots and the lookups."""
    value = tree.get(SHAPE)
    if value is None or len(value) != 2:
        return LIGHTS, True
    return max(1, min(LIGHTS, int(value[0]))), bool(value[1])


def reshape(tree, lights=None, own=None, exact=False):
    """Give the models' group what the scene needs: a light slot per light
    that paints rims (at least MIN_LIGHTS) and the per-model lookups once a
    model has settings of its own. Unused parts cost every outlined pixel
    and every shader compile, for exactly the same picture (an empty slot
    adds zero, a model without its own settings reads the scene's).

    It only grows while working, so a slider or a light switched off never
    rebuilds it twice; exact (on load) also drops what is no longer used.
    Returns whether the group was rebuilt (its materials compile again)."""
    if tree is None or tree.get(KIND) != 'lines':
        return False
    slots, reads_own = shape(tree)
    wanted_slots = slots if lights is None else max(MIN_LIGHTS, min(LIGHTS, int(lights)))
    wanted_own = reads_own if own is None else bool(own)
    if not exact:
        wanted_slots, wanted_own = max(wanted_slots, slots), wanted_own or reads_own
    if (wanted_slots, wanted_own) == (slots, reads_own) and tree.get(SHAPE) is not None:
        return False
    tree[SHAPE] = (wanted_slots, int(wanted_own))
    build(tree)
    return True


def groups():
    """Both shared groups that exist: every outlined material uses one of them."""
    return [g for g in bpy.data.node_groups if g.get(TAG) and g.get(KIND) in ('lines', 'world')]


def set_value(target, name, value):
    """Leave equal values alone: any write tags every material for update."""
    old = getattr(target, name)
    if isinstance(value, (tuple, list)):
        same = len(old) == len(value) and all(abs(a - b) <= 1e-7 * max(1., abs(b)) for a, b in zip(old, value))
    elif isinstance(value, float):
        same = abs(old - value) <= 1e-7 * max(1., abs(value))
    else:
        same = old == value
    if not same:
        setattr(target, name, value)


def write_values(tree, values=(), colors=(), vectors=()):
    nodes = tree.nodes
    for name, item in dict(values).items():
        node = nodes.get(name)
        if node is not None:
            set_value(node.outputs[0], 'default_value', float(item))
    for name, item in colors:
        node = nodes.get(name)
        if node is not None:
            set_value(node.outputs[0], 'default_value', (*[float(c) for c in item[:3]], 1.))
    for name, item in vectors:
        node = nodes.get(name)
        if node is not None:
            for socket, part in zip(node.inputs, item):
                set_value(socket, 'default_value', float(part))


# ---------------------------------------------------------------- edge data

def _quads(obj):
    """The mesh's quads: (loops (Q, 4), corners (Q, 4, 3), copies (K, 2)).

    Triangulated faces count too: two triangles sharing their longest edge
    that together make a flat rectangle are measured as that rectangle (glTF,
    OBJ and FBX exports split every cuboid face this way). Their first loop of
    each corner is in loops; copies lists (other loop, loop it copies) for the
    shared edge's second pair of loops, which take the same values.
    """
    import numpy as np
    mesh = obj.data
    count = len(mesh.polygons)
    totals = np.empty(count, np.int32)
    starts = np.empty(count, np.int32)
    mesh.polygons.foreach_get('loop_total', totals)
    mesh.polygons.foreach_get('loop_start', starts)
    corners = np.empty(len(mesh.loops), np.int32)
    mesh.loops.foreach_get('vertex_index', corners)
    points = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get('co', points)
    points = points.reshape(-1, 3).astype(np.float64)
    quads = np.nonzero(totals == 4)[0]
    loops = starts[quads, None] + np.arange(4)[None, :]
    paired, copies = _triangle_quads(totals, starts, corners, points)
    if len(paired):
        loops = np.concatenate([loops, paired])
    return loops, points[corners[loops]], copies


def _triangle_quads(totals, starts, corners, points, tolerance=1e-4):
    """Rectangles made of two triangles: (loops (P, 4), copies (K, 2)); see _quads."""
    import numpy as np
    none = np.zeros((0, 4), np.int64), np.zeros((0, 2), np.int64)
    triangles = np.nonzero(totals == 3)[0]
    if len(triangles) < 2:
        return none
    # Corners are matched by position: exporters often give each face its own vertices.
    place = np.unique(np.round(points / 1e-5).astype(np.int64), axis=0, return_inverse=True)[1].reshape(-1)
    tri_loops = starts[triangles, None] + np.arange(3)[None, :]          # (T, 3)
    tri_points = points[corners[tri_loops]]                             # (T, 3, 3)
    tri_place = place[corners[tri_loops]]                               # (T, 3)
    # Edge k runs from corner k to corner k + 1; the pair shares its longest.
    sides = np.linalg.norm(np.roll(tri_points, -1, axis=1) - tri_points, axis=2)
    longest = np.argmax(sides, axis=1)
    count = len(triangles)
    first = tri_place[np.arange(count), longest]
    second = tri_place[np.arange(count), (longest + 1) % 3]
    keys = np.stack([np.minimum(first, second), np.maximum(first, second)], axis=1)
    order = np.lexsort((keys[:, 1], keys[:, 0]))
    same = np.all(keys[order[1:]] == keys[order[:-1]], axis=1)
    lone = np.ones(len(order) - 1, bool)  # an edge three triangles share pairs none of them
    lone[1:] &= ~same[:-1]
    lone[:-1] &= ~same[1:]
    pairs = np.nonzero(same & lone)[0]
    if not len(pairs):
        return none
    one, two = order[pairs], order[pairs + 1]
    # Corners in winding order: a (one's corner off the edge), u, b (two's), v.
    a_slot = (longest[one] + 2) % 3
    u_slot, v_slot = longest[one], (longest[one] + 1) % 3
    b_slot = (longest[two] + 2) % 3
    a, u, v = (tri_points[one, a_slot], tri_points[one, u_slot], tri_points[one, v_slot])
    b = tri_points[two, b_slot]
    size = np.maximum(np.linalg.norm(v - u, axis=1), 1e-12)
    legs = (np.einsum('ij,ij->i', u - a, v - a) / size ** 2, np.linalg.norm(u + v - a - b, axis=1) / size)
    # The same edge must run the other way in the second triangle (one surface).
    turned = tri_place[two, longest[two]] == tri_place[one, v_slot]
    normal_one = np.cross(u - a, v - a)
    normal_two = np.cross(v - b, u - b)
    facing = np.einsum('ij,ij->i', normal_one, normal_two) > 0
    good = (np.abs(legs[0]) <= tolerance) & (legs[1] <= tolerance) & turned & facing
    if not np.any(good):
        return none
    one, two = one[good], two[good]
    a_slot, u_slot, v_slot, b_slot = a_slot[good], u_slot[good], v_slot[good], b_slot[good]
    quad = np.stack([tri_loops[one, a_slot], tri_loops[one, u_slot],
                     tri_loops[two, b_slot], tri_loops[one, v_slot]], axis=1)
    # The second triangle's loops on the shared edge copy the first's.
    two_u = tri_loops[two, (longest[two] + 1) % 3]
    two_v = tri_loops[two, longest[two]]
    copies = np.concatenate([np.stack([two_u, tri_loops[one, u_slot]], axis=1),
                             np.stack([two_v, tri_loops[one, v_slot]], axis=1)])
    return quad, copies


def compute(objects, tolerance=outline_data.TOLERANCE, merge=()):
    """Edge data for meshes that touch each other: {mesh: (loops, values, thickness)}.

    Adjacency is found in world space across every object given, so blocks
    split into separate meshes (one per material in MiEx) still join up.
    Objects in merge (models and rigs, not world blocks) have their faces
    measured whole even when cut into strips, as bendy player rigs are.
    """
    import numpy as np
    merging = {obj.as_pointer() for obj in merge}
    parts, seen = [], set()
    for obj in objects:
        mesh = obj.data
        if mesh is None or mesh.as_pointer() in seen:
            continue
        seen.add(mesh.as_pointer())
        loops, local, copies = _quads(obj)
        if not len(loops):
            continue
        matrix = np.array(obj.matrix_world, np.float64)
        world = local @ matrix[:3, :3].T + matrix[:3, 3]
        parts.append((mesh, loops, local, world, matrix, obj.as_pointer() in merging, copies))
    if not parts:
        return {}
    kinds, thickness, box, lines = outline_data.classify_regions(
        np.concatenate([part[3] for part in parts]),
        np.concatenate([np.full(len(part[1]), part[5]) for part in parts]), tolerance)
    result, start = {}, 0
    for mesh, loops, local, world, matrix, merged, copies in parts:
        stop = start + len(loops)
        # A strip measures from its face's far edge, which may lie on another
        # strip: bring those edges into this mesh's own space.
        ends = None
        if merged and abs(np.linalg.det(matrix[:3, :3])) > 1e-12:
            inverse = np.linalg.inv(matrix)
            ends = lines[start:stop] @ inverse[:3, :3].T + inverse[:3, 3]
        values = outline_data.corner_values(local, kinds[start:stop], box[start:stop], FAR, ends)
        # Thickness is measured in world units; the shader scales object
        # distances to world, so store it in object units like the rest.
        ratio = (np.linalg.norm(local[:, 1] - local[:, 0], axis=1) /
                 np.maximum(np.linalg.norm(world[:, 1] - world[:, 0], axis=1), 1e-12))
        result[mesh] = (loops, values, (thickness[start:stop] * ratio).astype(np.float32), copies)
        start = stop
    return result


def write(result):
    import numpy as np
    for mesh, (loops, values, thickness, copies) in result.items():
        for name, kind in ((ATTR, 'FLOAT_VECTOR'), (ATTR_LAST, 'FLOAT'), (ATTR_DEPTH, 'FLOAT')):
            attribute = mesh.attributes.get(name)
            if attribute is not None and (attribute.domain != 'CORNER' or attribute.data_type != kind):
                mesh.attributes.remove(attribute)
                attribute = None
            if attribute is None:
                mesh.attributes.new(name, kind, 'CORNER')
        flat = loops.reshape(-1)
        vectors = np.zeros((len(mesh.loops), 3), np.float32)
        vectors[flat] = values[:, :, :3].reshape(-1, 3)
        fourth = np.zeros(len(mesh.loops), np.float32)
        fourth[flat] = values[:, :, 3].reshape(-1)
        depth = np.zeros(len(mesh.loops), np.float32)
        depth[flat] = np.repeat(thickness, 4)
        if len(copies):
            # A triangulated rectangle's second triangle shares the first's corner values.
            for array in (vectors, fourth, depth):
                array[copies[:, 0]] = array[copies[:, 1]]
        mesh.attributes[ATTR].data.foreach_set('vector', vectors.reshape(-1))
        mesh.attributes[ATTR_LAST].data.foreach_set('value', fourth)
        mesh.attributes[ATTR_DEPTH].data.foreach_set('value', depth)
        mesh[MARK] = DATA_VERSION
        mesh.update()


def clear(mesh):
    for name in (ATTR, ATTR_LAST, ATTR_DEPTH):
        attribute = mesh.attributes.get(name)
        if attribute is not None:
            mesh.attributes.remove(attribute)
    if MARK in mesh:
        del mesh[MARK]
    mesh.update()


def has_data(obj):
    return obj.type == 'MESH' and obj.data is not None and bool(obj.data.get(MARK)) and ATTR in obj.data.attributes


# ---------------------------------------------------------------- targets

def _names(obj):
    names = [obj.name]
    for slot in obj.material_slots:
        material = slot.material
        if material is not None:
            names.append(material.name)
            if material.node_tree:
                names += [n.image.name for n in material.node_tree.nodes if n.type == 'TEX_IMAGE' and n.image]
    return ' '.join(names).lower()


def eligible(obj):
    if obj.type != 'MESH' or obj.data is None or obj.library or (obj.data.library and not obj.data.override_library):
        return False
    if obj.get('mc_sky_owner') or obj.get('mcbedrock_is_emitter') or obj.get(OPT) is False:
        return False
    return not SKIP.search(_names(obj))


def expand(objects):
    """Meshes under the given objects: rig roots, armatures, empties and instances included."""
    found, pending, seen = [], list(objects), set()
    while pending:
        obj = pending.pop()
        if obj is None or obj.as_pointer() in seen:
            continue
        seen.add(obj.as_pointer())
        if obj.type == 'MESH':
            found.append(obj)
        if obj.instance_type == 'COLLECTION' and obj.instance_collection:
            pending.extend(obj.instance_collection.all_objects)
        pending.extend(obj.children)
    return [obj for obj in dict.fromkeys(found) if eligible(obj)]


def world_collections(scene=None):
    collections = [c for c in bpy.data.collections if c.get('mcaddon_miex_export')]
    if scene is not None:
        inside = {c.as_pointer() for c in scene.collection.children_recursive}
        collections = [c for c in collections if c.as_pointer() in inside]
    return collections


def in_world(obj):
    return any(obj in subtree_objects(collection) for collection in world_collections())


def outlined_objects(scene):
    return [obj for obj in scene.objects if has_data(obj)]


# ---------------------------------------------------------------- materials

def _principled(tree):
    return [n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED']


def remove_legacy(material):
    """Undo the ray-traced outlines (graph versions 1..6) on one material."""
    tree = material.node_tree
    if tree is None:
        return False
    changed = False
    node = tree.nodes.get(LEGACY_NODE)
    if node is not None and node.type == 'GROUP':
        port = node.inputs['Color']
        source = port.links[0].from_socket if port.links else None
        for link in list(node.outputs['Color'].links):
            if source is not None:
                tree.links.new(source, link.to_socket)
            else:
                link.to_socket.default_value = port.default_value
        tree.nodes.remove(node)
        changed = True
    for item in list(tree.nodes):
        if (item.name.startswith(LEGACY_ADD) and item.type == 'ADD_SHADER') or item.name == LEGACY_AOV:
            source = item.inputs[0].links[0].from_socket if item.inputs[0].links else None
            for link in list(item.outputs[0].links):
                if source is not None:
                    tree.links.new(source, link.to_socket)
            tree.nodes.remove(item)
            changed = True
        elif item.name == LEGACY_GLOW:
            tree.nodes.remove(item)
            changed = True
    if LEGACY_MARKER in material:
        del material[LEGACY_MARKER]
        changed = True
    return changed


def _ours(tree):
    return [n for n in tree.nodes if n.type == 'GROUP' and n.name.startswith(NODE)]


def attached(material):
    tree = material.node_tree
    return tree is not None and bool(_ours(tree))


def intact(material, group):
    tree = material.node_tree
    shaders = _principled(tree)
    nodes = _ours(tree)
    if not shaders or len(nodes) != len(shaders):
        return False
    for node in nodes:
        if node.node_tree != group:
            return False
        port = next((link.to_socket for link in node.outputs['Color'].links), None)
        if port is None or port.node.type != 'BSDF_PRINCIPLED' or port.name != 'Base Color':
            return False
    # Older attachments fed the smoothed shading normal: attach again.
    if any(n.name.startswith(NORMAL) and n.outputs['Normal'].links for n in tree.nodes if n.type == 'NEW_GEOMETRY'):
        return False
    return (tree.nodes.get(GLOW) is not None and tree.nodes.get(PASS_NODE) is not None
            and tree.nodes.get(PASS_NODE + ' light') is not None and tree.nodes.get(PASS_NODE + ' lines') is not None)


def detach(material):
    """Put the material back exactly as it was before attach()."""
    tree = material.node_tree
    if tree is None:
        return
    for node in _ours(tree):
        for name in ('Color', 'Normal'):
            port = node.inputs[name]
            source = port.links[0].from_socket if port.links else None
            if source is not None and source.node.name.startswith(NORMAL):
                source = None   # our own geometry normal: leave the input unlinked
            for link in list(node.outputs[name].links):
                target = link.to_socket
                tree.links.remove(link)
                if source is not None:
                    tree.links.new(source, target)
                elif name == 'Color':
                    target.default_value = port.default_value
        tree.nodes.remove(node)
    for item in list(tree.nodes):
        if item.name.startswith(ADD) and item.type == 'ADD_SHADER':
            source = item.inputs[0].links[0].from_socket if item.inputs[0].links else None
            for link in list(item.outputs[0].links):
                if source is not None:
                    tree.links.new(source, link.to_socket)
            tree.nodes.remove(item)
        elif item.name == GLOW or item.name.startswith(NORMAL) or item.name.startswith(PASS_NODE):
            tree.nodes.remove(item)


def attach(material, group):
    """Insert the shared group before every Principled BSDF's Base Color and
    Normal, add the rim glow beside the surface and write the stroke passes."""
    tree = material.node_tree
    shaders = _principled(tree)
    if not shaders:
        return False
    nodes, links = tree.nodes, tree.links
    soft = bool(SOFT.search((material.name + ' ' + ' '.join(
        n.image.name for n in nodes if n.type == 'TEX_IMAGE' and n.image)).lower()))
    first = None
    for index, shader in enumerate(shaders):
        node = nodes.new('ShaderNodeGroup')
        node.name = NODE + ('' if not index else ' ' + str(index))
        node.label = 'Outlines · original color kept'
        node.node_tree = group
        node.location = (shader.location.x - 240, shader.location.y + 280)
        node.inputs['Strength'].default_value = .5 if soft else 1.
        for port_name, input_name in (('Base Color', 'Color'), ('Alpha', 'Alpha')):
            port = shader.inputs[port_name]
            if port.links:
                links.new(port.links[0].from_socket, node.inputs[input_name])
            else:
                node.inputs[input_name].default_value = port.default_value
        links.new(node.outputs['Color'], shader.inputs['Base Color'])
        normal = shader.inputs['Normal']
        if normal.links:
            links.new(normal.links[0].from_socket, node.inputs['Normal'])
        else:
            geometry = nodes.new('ShaderNodeNewGeometry')
            geometry.name = NORMAL + ('' if not index else ' ' + str(index))
            geometry.location = (node.location.x - 200, node.location.y - 120)
            # True Normal: block faces stay perfectly flat even when a model
            # carries smoothed or custom normals (hexioq's fix for heads).
            links.new(geometry.outputs['True Normal'], node.inputs['Normal'])
        links.new(node.outputs['Normal'], normal)
        first = first or node
    emission = nodes.new('ShaderNodeEmission')
    emission.name = GLOW
    emission.location = (first.location.x + 380, first.location.y)
    links.new(first.outputs['Emission'], emission.inputs['Color'])
    for index, output in enumerate(n for n in nodes if n.type == 'OUTPUT_MATERIAL'):
        surface = output.inputs['Surface']
        if not surface.links:
            continue
        add = nodes.new('ShaderNodeAddShader')
        add.name = ADD + ('' if not index else ' ' + str(index))
        add.location = (output.location.x - 180, output.location.y + 140)
        links.new(surface.links[0].from_socket, add.inputs[0])
        links.new(emission.outputs[0], add.inputs[1])
        links.new(add.outputs[0], surface)
    # Both passes are color AOVs: in Blender 5.2 EEVEE a value AOV that comes
    # first on the view layer renders empty.
    for suffix, pass_name, socket_name, port in (('', STROKE_PASS, 'Stroke mask', 'Color'),
                                                 (' light', STROKE_LIGHT_PASS, 'Stroke light', 'Color'),
                                                 (' lines', LINE_PASS, 'Line masks', 'Color')):
        aov = nodes.new('ShaderNodeOutputAOV')
        aov.name = PASS_NODE + suffix
        aov.aov_name = pass_name
        aov.location = (first.location.x + 380, first.location.y - 140 - 120 * bool(suffix))
        links.new(first.outputs[socket_name], aov.inputs[port])
    return True


def lined(obj):
    """Models, rigs and world blocks picked out for lines: they read the Outlines page."""
    return not obj.get(WORLD_FLAG) or bool(obj.get(MODEL_FLAG))


def material_wanted(material):
    if material is None or material.node_tree is None or material.library:
        return False
    if material.get('mc_outline_exclude') or not _principled(material.node_tree):
        return False
    return True


def sync(scene, group=None):
    """Every material on an outlined mesh carries a shared group; no other does.

    Materials on models get GROUP, which reads each model's own settings;
    materials only world blocks use get WORLD_GROUP. Unchanged materials are
    left alone, so this is safe before every render.
    """
    wanted = {}       # material pointer -> (material, used by a model)
    own = False
    for obj in outlined_objects(scene):
        model = lined(obj)
        own = own or (model and has_own(obj))
        for slot in obj.material_slots:
            if material_wanted(slot.material):
                key = slot.material.as_pointer()
                wanted[key] = (slot.material, model or (key in wanted and wanted[key][1]))
    trees, elsewhere = {}, None
    for material in bpy.data.materials:
        if material.library or material.node_tree is None:
            continue
        key = material.as_pointer()
        if key in wanted:
            model = wanted[key][1]
            if not model:
                # A world material that a model in another scene shares reads models' settings.
                if elsewhere is None:
                    elsewhere = _wanted_elsewhere(scene)
                model = elsewhere.get(key, False)
            if model not in trees:
                trees[model] = find_group(create=True, world=not model)
            if not intact(material, trees[model]):
                if attached(material):
                    detach(material)
                attach(material, trees[model])
        elif attached(material):
            if elsewhere is None:
                elsewhere = _wanted_elsewhere(scene)
            if key not in elsewhere:
                detach(material)
    _signatures[scene.as_pointer()] = _signature(scene)
    # A model with settings of its own (appended, or from before): read them.
    if own and reshape(trees.get(True) or find_group(create=False), own=True):
        update(scene, force=True)       # a rebuilt group starts from its defaults


def _wanted_elsewhere(scene):
    """Materials on outlined meshes in other scenes keep their group: {pointer: used by a model}."""
    found = {}
    for other in bpy.data.scenes:
        if other == scene:
            continue
        for obj in outlined_objects(other):
            model = lined(obj)
            for slot in obj.material_slots:
                if slot.material:
                    key = slot.material.as_pointer()
                    found[key] = found.get(key, False) or model
    return found


def remove_legacy_everywhere():
    changed = False
    for material in bpy.data.materials:
        if material.library is None and remove_legacy(material):
            changed = True
    for tree in list(bpy.data.node_groups):
        if tree.get('mc_lighting_kind') == 'Outlines' and not tree.users:
            bpy.data.node_groups.remove(tree)
    return changed


def prepare(scene):
    """Called after Sky & Lighting replaces or restores material copies."""
    remove_legacy_everywhere()
    sync(scene)
    update(scene)


# ---------------------------------------------------------------- scene

def add(scene, objects, stroke=None, picked=()):
    """Compute edge data for objects (jointly) and outline their materials.

    Models and rigs join the compositor stroke; world blocks do not. World
    blocks in picked (trees, say) keep their world edges untouched and get
    model lines of their own on top, switched off until set on the Outlines
    page.
    """
    meshes = expand(objects)
    if not meshes:
        return 0
    world = {obj.as_pointer() for collection in world_collections() for obj in subtree_objects(collection)}
    kept, fresh = set(), []
    for obj in picked:
        if obj.type == 'MESH' and obj.as_pointer() in world:
            if not obj.get(MODEL_FLAG):
                fresh.append(obj)
            obj[MODEL_FLAG] = 1.
            if has_data(obj):  # measured with its world: keep those edges exactly
                kept.add(obj.as_pointer())
    measured = [obj for obj in meshes if obj.as_pointer() not in kept]
    if measured:
        write(compute(measured, merge=[obj for obj in measured if obj.as_pointer() not in world]))
    for obj in meshes:
        if obj.get(OPT) is not None:
            del obj[OPT]
        is_world = obj.as_pointer() in world
        obj[STROKE_FLAG] = 0. if (stroke is False or (stroke is None and is_world)) else 1.
        obj[WORLD_FLAG] = 1. if is_world else 0.
    for obj in fresh:
        make_own(obj, scene, lines_off=True)
    sync(scene)
    update(scene, force=True)
    # Only meshes with faces to measure carry lines (a sphere, an n-gon slab: none).
    return sum(1 for obj in meshes if has_data(obj))


def remove(scene, objects=None):
    """Remove edge data (and lines) from objects, or from every outlined object.

    Their own settings go too: outlines added again start from the scene's."""
    meshes = outlined_objects(scene) if objects is None else [o for o in expand(objects) if has_data(o)]
    for obj in meshes:
        clear(obj.data)
        for flag in (STROKE_FLAG, WORLD_FLAG, MODEL_FLAG):
            if flag in obj:
                del obj[flag]
        if has_own(obj):
            drop_own(obj)
    sync(scene)
    from . import outline_stroke
    if not outlined_objects(scene):
        for group in groups():
            if not group.users:
                bpy.data.node_groups.remove(group)
        outline_stroke.remove(scene)
    else:
        sync_stroke(scene)
    return len(meshes)


def set_world(scene, collection, enabled):
    """Opt one imported MiEx world in or out; returns what the user will see."""
    collection[WORLD] = bool(enabled)
    meshes = [obj for obj in subtree_objects(collection) if obj.type == 'MESH']
    if enabled:
        count = add(scene, meshes, stroke=False)
        return f'outlines on {count} meshes' if count else 'no blocks to outline'
    remove(scene, meshes)
    return 'outlines removed'


def refresh(scene, objects=None):
    """Recompute edge data after geometry edits: for objects (rig roots stand
    for their parts), or every outlined object. A world is always measured
    whole, so one of its blocks brings the rest of it."""
    outlined = outlined_objects(scene)
    pointers = {obj.as_pointer() for obj in outlined}
    picked = pointers if objects is None else {obj.as_pointer() for obj in expand(objects)} & pointers
    done = set()
    for collection in world_collections(scene):
        members = [obj for obj in subtree_objects(collection) if obj.as_pointer() in pointers]
        if members and any(obj.as_pointer() in picked for obj in members):
            write(compute(members))
            done.update(obj.as_pointer() for obj in members)
    rest = [obj for obj in outlined if obj.as_pointer() in picked and obj.as_pointer() not in done]
    if rest:
        write(compute(rest, merge=[obj for obj in rest if not obj.get(WORLD_FLAG)]))
    if done or rest:
        sync(scene)
        update(scene, force=True)
    return len(done) + len(rest)


def upgrade(scene):
    """Recompute models outlined before DATA_VERSION 2, whose faces cut into
    strips (bendy rigs) lost face shading and their full-width lines."""
    models = [obj for obj in outlined_objects(scene) if not obj.get(WORLD_FLAG) and not obj.data.library]
    if not any(obj.data.get(MARK, 0) < DATA_VERSION for obj in models):
        return 0
    write(compute(models, merge=models))
    return len(models)


def pixel_scale(scene):
    """One pixel of a 1080-pixel short side: (per unit of view depth, constant)."""
    render = scene.render
    width = max(1., render.resolution_x * render.pixel_aspect_x)
    height = max(1., render.resolution_y * render.pixel_aspect_y)
    short, large = min(width, height), max(width, height)
    camera = scene.camera.data if scene.camera and scene.camera.type == 'CAMERA' else None
    if camera is not None and camera.type == 'ORTHO':
        return 0., camera.ortho_scale * short / large / 1080
    if camera is None or camera.type != 'PERSP':
        half = 18 / 50  # 50 mm on a 36 mm sensor, the viewport default
        return 2 * half * short / large / 1080, 0.
    fit = camera.sensor_fit
    if fit == 'AUTO':
        half = camera.sensor_width / (2 * camera.lens) * short / large
    else:
        size = camera.sensor_width if fit == 'HORIZONTAL' else camera.sensor_height
        half = size / (2 * camera.lens)
        if (fit == 'HORIZONTAL') == (width > height):
            half *= short / large
    return 2 * half / 1080, 0.


_rendering = set()       # scenes rendering: their lines follow the scene camera


def view_scale(scene):
    """pixel_scale for the largest viewport showing materials, unless it looks
    through the camera (or nothing is open, or the scene is rendering).

    Widths are pixels of a 1080-pixel short side of the view you are looking
    through. Using the camera's lens in a free viewport made lines too thick
    (or thin) whenever the view's lens or zoom differed from the camera's.
    """
    if bpy.app.background or scene.as_pointer() in _rendering:
        return None
    manager = bpy.context.window_manager
    best = None
    for window in manager.windows if manager else ():
        if window.scene != scene:
            continue
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            space = area.spaces.active
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region is None or space.region_3d is None or space.shading.type not in {'MATERIAL', 'RENDERED'}:
                continue
            if best is None or region.width * region.height > best[0]:
                best = (region.width * region.height, space, region)
    if best is None:
        return None
    _size, space, region = best
    view = space.region_3d
    if view.view_perspective == 'CAMERA':
        return None
    # The viewport fits a 72 mm sensor to its longer side at the view's lens.
    short = min(region.width, region.height) / max(region.width, region.height, 1)
    half = 36. / max(space.lens, 1.) * short
    if view.view_perspective == 'ORTHO':
        return 0., 2 * half * view.view_distance / 1080
    return 2 * half / 1080, 0.


def _light_color(data):
    color = tuple(data.color)
    if getattr(data, 'use_temperature', False):
        color = tuple(c * t for c, t in zip(color, data.temperature_color))
    return color


def scene_parts(scene):
    """The outlined models and the lights, in one walk over the scene.

    The handlers run on every edit and frame; separate walks cost several
    times as much in a large world.
    """
    outlined, lights = [], []
    for obj in scene.objects:
        kind = obj.type
        if kind == 'LIGHT':
            lights.append(obj)
        elif kind == 'MESH' and has_data(obj):
            outlined.append(obj)
    return outlined, lights


def focus_point(scene, outlined=None):
    """The middle of what is outlined: where light falloff is measured from."""
    from mathutils import Vector
    objects = (outlined_objects(scene) if outlined is None else outlined)[:64]
    if not objects:
        return scene.camera.matrix_world.translation.copy() if scene.camera else Vector()
    centre = Vector()
    for obj in objects:
        centre += obj.matrix_world @ (sum((Vector(corner) for corner in obj.bound_box), Vector()) / 8)
    return centre / len(objects)


def light_extent(scene, lights, outlined=None):
    """Where Rim and Ink fade fall off from: (1 for a sun else 0, the
    direction toward the sun or the lamp's position, and how far from it the
    outlined models' nearest and farthest corners are). A posed rig's bounds
    follow its pose."""
    from mathutils import Vector
    if not lights:
        return 1., (0., 0., 1.), 0., 1.
    kind, where = lights[0][0], Vector(lights[0][1])
    sun = kind == 'SUN'
    models = [obj for obj in (outlined_objects(scene) if outlined is None else outlined)
              if not obj.get(WORLD_FLAG)][:64]
    distances = [-point.dot(where) if sun else (point - where).length
                 for obj in models for point in (obj.matrix_world @ Vector(corner) for corner in obj.bound_box)]
    if not distances:
        return float(sun), tuple(where), 0., 1.
    return float(sun), tuple(where), min(distances), max(distances)


def light_reference(lights, focus):
    """How brightly the strongest light reaches the focus, in rim units."""
    from mathutils import Vector
    best = 0.
    for kind, where, axis, rgb, cone, blend, _ in lights:
        brightness = sum(c * w for c, w in zip(rgb, LUMINANCE))
        if kind != 'SUN':
            offset = Vector(where) - focus
            brightness /= max(offset.length_squared, 1e-4)
            if kind == 'AREA':
                brightness *= max(-offset.normalized().dot(Vector(axis)), .25)
        best = max(best, brightness)
    return best or 1.


def scene_lights(scene, limit=LIGHTS, focus=None, light_objects=None):
    """The lights that paint rims, brightest at the camera first.

    Each is (kind, position or direction, emission axis, rgb, spot cone, spot
    blend, importance); rgb carries the light's color and strength as the
    radiance a white surface facing it would have. Sky & Lighting's helper
    fills and soft sun copies are left out; its sun and moon stand in.
    """
    from mathutils import Vector
    focus = focus_point(scene) if focus is None else focus
    found = []

    def entry(obj, energy, kind):
        data = obj.data
        matrix = obj.matrix_world if obj.parent or obj.constraints else obj.matrix_basis
        rotation = matrix.to_quaternion()
        color = _light_color(data)
        strength = energy * 2 ** getattr(data, 'exposure', 0.)
        axis = (rotation @ Vector((0, 0, -1))).normalized()
        cone, blend = -2., -1.5
        if kind == 'SUN':
            where, radiance, importance = -axis, strength / math.pi, strength
        else:
            where = matrix.translation.copy()
            distance2 = max((where - focus).length_squared, 1e-4)
            if kind == 'AREA':
                radiance = strength / math.pi ** 2
            else:
                radiance = strength / (4 * math.pi ** 2)
            if kind == 'SPOT':
                half = data.spot_size / 2
                cone, blend = math.cos(half), math.cos(half * (1 - data.spot_blend)) + 1e-4
            importance = radiance * math.pi / distance2
        rgb = tuple(c * radiance for c in color)
        found.append((kind, tuple(where), tuple(axis), rgb, cone, blend, importance * max(color)))

    rig = {}
    for obj in scene.objects if light_objects is None else light_objects:
        if obj.type != 'LIGHT' or obj.hide_render or obj.get('mc_outline_ignore') or obj.data.energy <= 0:
            continue
        if obj.get('mc_sky_owner'):
            rig[obj.get('role', '')] = obj
            continue
        entry(obj, obj.data.energy, obj.data.type)
    # Sky & Lighting splits the sun over soft copies and adds fills; its sun
    # and moon carry the rim.
    for role in ('Sun', 'Moon'):
        obj = rig.get(role)
        if obj is not None:
            energy = obj.data.energy
            if role == 'Sun':
                energy += sum(rig[name].data.energy for name in ('Sun soft 1', 'Sun soft 2', 'Sun soft 3') if name in rig)
            entry(obj, energy, 'SUN')
    found.sort(key=lambda item: -item[-1])
    return found[:limit]


def light_slots(scene, light_objects=None):
    """How many lights could paint rims: scene_lights without its strength
    test, so a light animated up from zero already has its slot and a render
    never has to rebuild the group."""
    count, rig = 0, set()
    for obj in scene.objects if light_objects is None else light_objects:
        if obj.type != 'LIGHT' or obj.hide_render or obj.get('mc_outline_ignore'):
            continue
        if obj.get('mc_sky_owner'):
            if obj.get('role', '') in ('Sun', 'Moon'):
                rig.add(obj.get('role'))
            continue
        count += 1
    return min(LIGHTS, count + len(rig))


def line_values(s):
    """The group's line values for one set of settings, the scene's or a model's own.

    {name: value}: numbers, (r, g, b) colors, and each layer's blend mode as
    its index in BLENDS[layer] under '<layer> blend'.
    """
    size = s.line_size
    values = {
        'Ink width': s.ink_width * size if s.ink else 0.,
        'Ink opacity': s.ink_opacity if s.ink else 0.,
        'Ink shadow side': 1. if s.ink_side == 'SHADOW' else 0.,
        'Ink custom': 1. if s.ink_color_mode == 'CUSTOM' else 0.,
        'Ink depth': s.ink_depth,
        'Rim width': s.rim_width * size if s.rim else 0.,
        'Rim opacity': s.rim_opacity if s.rim else 0.,
        'Rim spread': s.rim_spread,
        'Rim custom': 0. if s.rim_color_mode == 'SURFACE' else 1.,
        'Rim light color': 1. if s.rim_color_mode == 'LIGHT' else s.rim_light_tint,
        'Rim glow': s.rim_glow if s.rim else 0.,
        'Rim fade': s.rim_fade, 'Rim taper': s.rim_taper, 'Rim gradient': s.rim_gradient,
        'Light falloff': s.light_falloff,
        'Ink fade': s.ink_fade, 'Ink taper': s.ink_taper, 'Ink gradient': s.ink_gradient,
        'Edge width': s.edge_width * size if s.edges else 0.,
        'Edge highlight': s.edge_highlight if s.edges else 0.,
        'Edge shade': s.edge_shade if s.edges else 0.,
        'Edge follow light': s.edge_follow_light,
        'Bevel size': s.bevel_size,
        'Bevel strength': s.bevel_strength if s.bevel else 0.,
        'Bevel light': s.bevel_light if s.bevel else 0.,
        'Gradient': s.gradient,
        'Weight variation': s.weight_variation,
        'Stroke': 1. if s.stroke else 0.,
        'Rim color': tuple(s.rim_color), 'Ink color': tuple(s.ink_color), 'Edge color': tuple(s.edge_color),
        'Rim color 2': tuple(s.rim_color_2), 'Ink color 2': tuple(s.ink_color_2),
    }
    for layer, field in BLEND_FIELDS:
        values[layer + ' blend'] = float(BLENDS[layer].index(getattr(s, field)))
    return values


def update(scene, force=False, parts=None):
    """Write the scene's settings and lights into the shared groups; cheap when unchanged."""
    trees = groups()
    s = getattr(scene, 'mcbedrock_outlines', None)
    if not trees or s is None:
        return
    for tree in trees:
        if tree.get(TAG) != VERSION:
            build(tree)
    per_depth, constant = view_scale(scene) or pixel_scale(scene)
    render = scene.render
    aa = 1080 / max(1., render.resolution_y * render.resolution_percentage / 100)
    outlined, light_objects = scene_parts(scene) if parts is None else parts
    focus = focus_point(scene, outlined)
    lights = scene_lights(scene, focus=focus, light_objects=light_objects)
    for tree in trees:
        reshape(tree, lights=light_slots(scene, light_objects))
    vectors, colors, values = [], [], {}
    for index in range(LIGHTS):
        names = light_names(index)
        if index < len(lights):
            kind, where, axis, rgb, cone, blend, _ = lights[index]
        else:
            kind, where, axis, rgb, cone, blend = 'SUN', (0., 0., 1.), (0., 0., -1.), (0., 0., 0.), -2., -1.5
        vectors += [(names['position'], where), (names['axis'], axis)]
        colors.append((names['color'], rgb))
        values.update({names['sun']: float(kind == 'SUN'), names['area']: float(kind == 'AREA'),
                       names['cone']: cone, names['cone blend']: blend})
    values['Light reference'] = light_reference(lights, focus)
    if lights:
        from mathutils import Vector
        kind, where = lights[0][0], Vector(lights[0][1])
        if kind != 'SUN':
            where = (where - focus).normalized()
        vectors.append((KEY, tuple(where)))
    sun, source, nearest, farthest = light_extent(scene, lights, outlined)
    vectors.append((FADE_LIGHT, source))
    values.update({'Fade light sun': sun, 'Fade near': nearest, 'Fade far': farthest})
    line = line_values(s)
    colors += [(name, line.pop(name)) for name in OWN_COLORS]
    for layer, _field in BLEND_FIELDS:
        chosen = int(line.pop(layer + ' blend'))
        for index, mode in enumerate(BLENDS[layer]):
            values['%s blend %s' % (layer, mode)] = float(index == chosen)
    values.update(line)
    values.update({
        'Pixel per depth': per_depth,
        'Pixel constant': constant,
        'Pixel AA': min(aa, 3.),
        'Fade start': s.fade_distance,
        'Fade end': s.fade_distance * 3.5,
        'World lines': s.world_lines,
        'World bevel': s.world_lines,
        'World bevel light': s.world_lines * .6,
        'Debug masks': 1. if s.show_masks else 0.,
    })
    for tree in trees:
        write_values(tree, values, colors, vectors)
    from . import outline_stroke
    outline_stroke.update(scene, outlined)


# ---------------------------------------------------------------- settings

BUSY = set()


def reset(scene):
    """Every setting back to its default, with one update."""
    s = scene.mcbedrock_outlines
    BUSY.add(scene.as_pointer())
    try:
        for name in OutlineSettings.__annotations__:
            if name in ('stroke', 'style'):
                continue
            s.property_unset(name)
        s.property_unset('style')
    finally:
        BUSY.discard(scene.as_pointer())
    update(scene, force=True)


# One-click outline styles: settings that differ from the defaults. Edge
# lights is the defaults: thin light lines on edges and on the lit side of
# silhouettes, as in render artists' key art. Bold is a cartoon outline, one
# thick grey line round every shape and its inner edges. Sticker adds a
# stroke round the outside, lit by the lights.
STYLES = {
    'EDGES': {},
    'BOLD': dict(rim=False, ink_side='ALL', ink_color_mode='CUSTOM', ink_color=(.2, .2, .2), ink_width=6.,
                 ink_opacity=1., ink_fade=0., ink_taper=0., weight_variation=0., edge_width=3.,
                 edge_highlight_blend='MIX', edge_color=(.2, .2, .2)),
    'STICKER': dict(stroke=True, stroke_side='ALL', stroke_position='OUTSIDE', stroke_size=6., stroke_color=(1., 1., 1.)),
}


def apply_style(scene, key, models=()):
    """Every line setting back to its default, then the style's own values:
    on each of models (their own settings) or, with none, on the scene's."""
    from . import outline_stroke
    values = STYLES[key]
    if models:
        for obj in models:
            s = obj.mcbedrock_outlines
            BUSY.add(obj.as_pointer())
            try:
                for name in LINE_SETTINGS:
                    s.property_unset(name)
                for name, value in values.items():
                    if name in LINE_SETTINGS:
                        setattr(s, name, value)
                s.style = key
            finally:
                BUSY.discard(obj.as_pointer())
            write_own(obj)
        # Every model with a stroke shares its look: a style with one sets it.
        shared = {name: value for name, value in values.items() if name in SCENE_ONLY}
        if shared:
            BUSY.add(scene.as_pointer())
            try:
                for name, value in shared.items():
                    setattr(scene.mcbedrock_outlines, name, value)
            finally:
                BUSY.discard(scene.as_pointer())
        sync_stroke(scene)
        update(scene, force=True)
        return
    s = scene.mcbedrock_outlines
    BUSY.add(scene.as_pointer())
    try:
        for name in OutlineSettings.__annotations__:
            if name not in ('stroke', 'style'):
                s.property_unset(name)
        for name, value in values.items():
            if name != 'stroke':
                setattr(s, name, value)
        s.style = key
    finally:
        BUSY.discard(scene.as_pointer())
    stroke = values.get('stroke', False)
    if stroke != s.stroke:
        s.stroke = stroke
    elif stroke:
        outline_stroke.setup(scene)
    update(scene, force=True)


def settings_changed(self, context):
    owner = self.id_data
    if owner.as_pointer() in BUSY:
        return
    if isinstance(owner, bpy.types.Object):
        if has_own(owner):
            write_own(owner)
        return
    update(owner)


def stroke_changed(self, context):
    owner = self.id_data
    if owner.as_pointer() in BUSY:
        return
    if isinstance(owner, bpy.types.Object):
        if has_own(owner):
            write_own(owner)
            for scene in owner.users_scene:
                sync_stroke(scene)
                update(scene)
        return
    sync_stroke(owner)
    update(owner)


# ---------------------------------------------------------------- models' own settings

def has_own(obj):
    """Whether obj carries line settings of its own (else it uses the scene's)."""
    return OWN + '_0' in obj


def pack(values):
    """line_values() as the twelve 4-float properties the group reads (OWN_LAYOUT)."""
    values = dict(values, Own=1.)
    packed = []
    for names in OWN_LAYOUT:
        vector = []
        for name in names:
            vector.extend(values[name] if name in OWN_COLORS else (values[name],))
        packed.append(tuple(float(item) for item in vector))
    return packed


def write_own(obj):
    """Write obj's own settings where the shared group reads them (only what changed)."""
    changed = False
    for index, vector in enumerate(pack(line_values(obj.mcbedrock_outlines))):
        key = '%s_%d' % (OWN, index)
        old = obj.get(key)
        if old is None or len(old) != 4 or any(abs(a - b) > 1e-7 * max(1., abs(b)) for a, b in zip(old, vector)):
            obj[key] = vector
            changed = True
    if changed:
        # Custom properties reach renders and the viewport through the evaluated object.
        obj.update_tag(refresh={'OBJECT'})
    return changed


# Settings that draw a model's lines; zero draws none.
LINE_AMOUNTS = ('ink_opacity', 'rim_opacity', 'rim_glow', 'edge_highlight', 'edge_shade')


def make_own(obj, scene, lines_off=False):
    """Give obj line settings of its own: a copy of the scene's, its lines
    switched off with lines_off (world blocks picked out for lines)."""
    source, target = scene.mcbedrock_outlines, obj.mcbedrock_outlines
    BUSY.add(obj.as_pointer())
    try:
        for name in LINE_SETTINGS:
            setattr(target, name, 0. if lines_off and name in LINE_AMOUNTS else _plain(getattr(source, name)))
    finally:
        BUSY.discard(obj.as_pointer())
    write_own(obj)
    # The first model with settings of its own: the group starts reading them
    # (its materials compile once more), from the scene's values.
    if reshape(find_group(create=False), own=True):
        update(scene, force=True)


def drop_own(obj):
    """obj uses the scene's line settings again."""
    for index in range(len(OWN_LAYOUT)):
        key = '%s_%d' % (OWN, index)
        if key in obj:
            del obj[key]
    try:
        obj.property_unset('mcbedrock_outlines')
    except (AttributeError, TypeError):
        pass
    obj.update_tag(refresh={'OBJECT'})


def models(context):
    """The models the Outlines page edits: the active object's outlined meshes
    while it is selected (a rig root or armature stands for all its parts),
    the active object first. World blocks always use the scene's settings."""
    view_layer = getattr(context, 'view_layer', None)
    active = view_layer.objects.active if view_layer is not None else None
    if active is None or not active.select_get(view_layer=view_layer):
        return []
    found = [obj for obj in expand([active]) if has_data(obj) and lined(obj)]
    found.sort(key=lambda obj: obj != active)
    return found


def shown(scene, models, name=None):
    """The settings whose values the page shows: the first model's own, else the scene's."""
    if models and name not in SCENE_ONLY and has_own(models[0]):
        return models[0].mcbedrock_outlines
    return scene.mcbedrock_outlines


def stroke_wanted(scene, outlined=None):
    """Whether any model takes the compositor stroke: the scene's toggle, or a model's own."""
    if scene.mcbedrock_outlines.stroke:
        return True
    objects = outlined_objects(scene) if outlined is None else outlined
    return any(has_own(obj) and obj.mcbedrock_outlines.stroke for obj in objects)


def sync_stroke(scene):
    """Put the compositor stroke in while any model takes it, and take it out after."""
    from . import outline_stroke
    if stroke_wanted(scene):
        outline_stroke.setup(scene)
    elif outline_stroke.present(scene):
        outline_stroke.remove(scene)


def _plain(value):
    return tuple(value) if hasattr(value, '__len__') and not isinstance(value, str) else value


def _same(a, b):
    a, b = _plain(a), _plain(b)
    if isinstance(a, tuple):
        return len(a) == len(b) and all(abs(x - y) <= 1e-6 for x, y in zip(a, b))
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return abs(a - b) <= 1e-6


class Edit:
    """One setting changed on what the Outlines page edits, ready to be put back.

    The value goes to each model's own settings, or with no models (or for a
    scene-wide setting) to the scene's. A model that still uses the scene's
    settings gets its own copy first, unless the value is the scene's anyway.
    Sliders and the color picker preview through set() and revert(); the
    undoable operators make the change for good through set() alone.
    """

    def __init__(self, scene, models, name):
        self.scene, self.name = scene, name
        self.models = [] if name in SCENE_ONLY else list(models)
        self.made, self.before, self.switched = [], {}, []

    def _groups(self, value, layer):
        if not self.models:
            yield self.scene.mcbedrock_outlines
            return
        scene = self.scene.mcbedrock_outlines
        for obj in self.models:
            if not has_own(obj):
                if _same(getattr(scene, self.name), value) and not (layer and not getattr(scene, layer)):
                    continue
                make_own(obj, self.scene)
                self.made.append(obj)
            yield obj.mcbedrock_outlines

    def set(self, value, layer=None):
        """Show value now; layer (an on/off setting) is switched on where it is off."""
        for settings in self._groups(value, layer):
            key = settings.as_pointer()
            if key not in self.before:
                self.before[key] = (settings, _plain(getattr(settings, self.name)))
            if layer and not getattr(settings, layer):
                setattr(settings, layer, True)
                self.switched.append((settings, layer))
            if not _same(getattr(settings, self.name), value):
                setattr(settings, self.name, value)

    def revert(self):
        """Put back every value, switched layer and own copy this edit made."""
        made = {obj.as_pointer() for obj in self.made}
        for settings, value in self.before.values():
            if settings.id_data.as_pointer() not in made and not _same(getattr(settings, self.name), value):
                setattr(settings, self.name, value)
        for settings, layer in self.switched:
            if settings.id_data.as_pointer() not in made:
                setattr(settings, layer, False)
        for obj in self.made:
            drop_own(obj)
        self.made, self.before, self.switched = [], {}, []


def _float(name, default, low, high, description, soft=None):
    options = dict(name=name, description=description, default=default, min=low, max=high, update=settings_changed)
    if soft:
        options.update(soft_min=soft[0], soft_max=soft[1])
    return bpy.props.FloatProperty(**options)


def _color(name, default):
    return bpy.props.FloatVectorProperty(name=name, subtype='COLOR', size=3, default=default, min=0, max=1,
                                         update=settings_changed)


class OutlineSettings(bpy.types.PropertyGroup):
    style: bpy.props.EnumProperty(name='Style', default='EDGES', options={'HIDDEN'}, items=(
        ('EDGES', 'Edge lights', 'Thin light lines on edges and on the lit side'),
        ('BOLD', 'Bold', 'One thick grey line round every shape'),
        ('STICKER', 'Sticker', 'A stroke round the outside of each model')))
    line_size: _float('Line size', 1., .1, 4., 'Thicker or thinner: scales every line width at once')
    world_lines: _float('World edges', .5, 0., 1., 'How wide the soft edges on world blocks are; they also thin out as blocks get farther away')
    ink: bpy.props.BoolProperty(name='Ink', default=True, update=settings_changed,
                                description='Outline each shape in a deeper shade of its own color')
    ink_width: _float('Ink width', 1.25, 0., 24., 'Line width in pixels of a 1080-pixel-high image', soft=(0., 12.))
    ink_opacity: _float('Ink opacity', .85, 0., 1., 'How strongly ink covers the surface')
    ink_side: bpy.props.EnumProperty(name='Ink side', update=settings_changed, default='ALL', items=(
        ('SHADOW', 'Shadow side', 'Ink only the silhouettes turned away from the light'),
        ('ALL', 'All round', 'Ink every silhouette, under the rim')))
    ink_color_mode: bpy.props.EnumProperty(name='Ink color', update=settings_changed, default='SURFACE', items=(
        ('SURFACE', 'From surface', 'A deeper shade of each surface\'s own color'),
        ('CUSTOM', 'Custom', 'One ink color for every line')))
    ink_color: _color('Ink color', (.012, .008, .01))
    ink_depth: _float('Ink depth', .8, 0., 1., 'How much deeper than the surface its ink is')
    ink_blend: bpy.props.EnumProperty(name='Ink blend', items=blend_items('Ink'), default='MIX', update=settings_changed)
    ink_fade: _float('Ink fade', 0., 0., 1., 'Fade the ink out toward the light, smoothly over the whole model: at 100% it is gone where the model is nearest the light')
    ink_taper: _float('Ink taper', .6, 0., 1., 'Thin and fade the ink where it wraps round toward the light')
    ink_gradient: _float('Ink gradient', 0., 0., 1., 'Blend the ink toward a second color across its width')
    ink_color_2: _color('Ink inner color', (.2, .1, .06))
    rim: bpy.props.BoolProperty(name='Rim', default=True, update=settings_changed,
                                description='Paint and light up the silhouettes that face each light')
    rim_width: _float('Rim width', 1.5, 0., 24., 'Rim width in pixels of a 1080-pixel-high image', soft=(0., 14.))
    rim_opacity: _float('Rim opacity', .9, 0., 1., 'How strongly the rim covers the surface')
    rim_spread: _float('Rim reach', 0., -1., 1., 'How far round the shape the rim reaches from the lit side')
    light_falloff: _float('Light falloff', .6, 0., 1., 'Rims fade and thin with distance from point, spot and area lights, like the light itself')
    rim_color_mode: bpy.props.EnumProperty(name='Rim color', update=settings_changed, default='CUSTOM', items=(
        ('CUSTOM', 'Custom', 'One rim color, tinted by the lights'),
        ('SURFACE', 'From surface', 'A lighter shade of each surface'),
        ('LIGHT', 'Light color', 'The color of whichever lights reach the rim')))
    rim_color: _color('Rim color', (1., .96, .88))
    rim_light_tint: _float('Light tint', .5, 0., 1., 'How much each light\'s own color tints the rim it paints')
    rim_glow: _float('Rim glow', .2, 0., 8., 'Light the rim up with the lights that reach it', soft=(0., 3.))
    rim_blend: bpy.props.EnumProperty(name='Rim blend', items=blend_items('Rim'), default='MIX', update=settings_changed)
    rim_fade: _float('Rim fade', 0., 0., 1., 'Fade the rim out with distance from the light, smoothly over the whole model: at 100% it is gone where the model is farthest from the light')
    rim_taper: _float('Rim taper', .35, 0., 1., 'Thin and fade the rim where it wraps round away from the light')
    rim_gradient: _float('Rim gradient', 0., 0., 1., 'Blend the rim toward a second color across its width')
    rim_color_2: _color('Rim inner color', (.9, .55, .25))
    edges: bpy.props.BoolProperty(name='Inner edges', default=True, update=settings_changed,
                                  description='Lighter and darker lines along the edges inside a shape')
    edge_width: _float('Edge width', 1.5, 0., 12., 'Inner edge width in pixels of a 1080-pixel-high image')
    edge_highlight: _float('Edge highlight', 1., 0., 1., 'Lighter lines where an edge faces the light')
    edge_shade: _float('Edge shade', 0., 0., 1., 'Darker lines where an edge faces away')
    edge_color: _color('Edge light color', (1., 1., 1.))
    edge_follow_light: _float('Edges follow light', 0., 0., 1., 'At 1 only edges facing the light get the light line; at 0 every edge does, like painted edge lights')
    edge_highlight_blend: bpy.props.EnumProperty(name='Highlight blend', items=blend_items('Highlight'), default='SOFT_LIGHT',
                                                 update=settings_changed)
    edge_shade_blend: bpy.props.EnumProperty(name='Shade blend', items=blend_items('Shade'), default='MULTIPLY',
                                             update=settings_changed)
    bevel: bpy.props.BoolProperty(name='Bevel', default=False, update=settings_changed,
                                  description='Round edges so they catch real light and shadow')
    bevel_size: _float('Bevel size', .06, .005, .5, 'Bevel width in blocks', soft=(.01, .25))
    bevel_strength: _float('Bevel strength', .6, 0., 1., 'How round the edges look')
    bevel_light: _float('Bevel brightness', .15, 0., 1., 'Brighten the beveled edge itself')
    gradient: _float('Face gradient', 0., 0., 1., 'Shade each face from the corner facing the light to the far one')
    weight_variation: _float('Line weight', .3, 0., 1., 'Thicker lines on the shadow side, thinner toward the light')
    fade_distance: _float('Fade starts', 120., 1., 100000., 'Lines start to fade this far from the camera, in blocks, and are gone at 3.5 times it',
                          soft=(1., 1000.))
    show_masks: bpy.props.BoolProperty(name='Show line masks', default=False, update=settings_changed,
                                       description='Show rims in red, inner edges in green and ink in blue')
    # Compositor stroke around models (outline_stroke.py).
    stroke: bpy.props.BoolProperty(name='Stroke', default=False, update=stroke_changed,
                                   description='Draw a Photoshop-style stroke around outlined models in the compositor')
    stroke_side: bpy.props.EnumProperty(name='Side', update=settings_changed, default='LIGHT', items=(
        ('LIGHT', 'Lit side', 'Only where light reaches the outline, in the rim color'),
        ('ALL', 'All round', 'The whole outline: rim color where lit, stroke color elsewhere'),
        ('SHADOW', 'Shadow side', 'Only the side turned away from the light')))
    stroke_position: bpy.props.EnumProperty(name='Position', update=settings_changed, default='INSIDE', items=(
        ('INSIDE', 'On the edge', 'Painted on the model, along the inside of its outline'),
        ('CENTER', 'Center', 'Half on the model, half outside it'),
        ('OUTSIDE', 'Outside', 'Around the model, like a sticker border')))
    stroke_size: _float('Stroke size', 5., 0., 64., 'Stroke width in pixels of a 1080-pixel-high image', soft=(0., 24.))
    stroke_blend: bpy.props.EnumProperty(name='Stroke blend', items=blend_items('Rim'), default='MIX', update=settings_changed)
    stroke_opacity: _float('Stroke opacity', 1., 0., 1., 'How strongly the stroke covers what is behind it')
    stroke_color: _color('Stroke color', (.012, .008, .01))
    stroke_light: _float('Lit brightness', 1., 0., 8., "Brightness of the stroke's lit side, in the rim color", soft=(0., 3.))
    stroke_softness: _float('Stroke softness', 0., 0., 16., 'Feather the stroke\'s edges, in pixels', soft=(0., 6.))
    stroke_glow: _float('Outer glow', 0., 0., 128., 'A soft glow beyond the stroke, in pixels, colored by the lights', soft=(0., 48.))
    stroke_glow_strength: _float('Glow strength', .6, 0., 4., 'How bright the outer glow is', soft=(0., 2.))


# The settings a model can have its own of (on a model, stroke means "this model takes the stroke").
LINE_SETTINGS = tuple(name for name in OutlineSettings.__annotations__ if name not in SCENE_ONLY)


# ---------------------------------------------------------------- operators

class MCBEDROCK_OT_outlines(bpy.types.Operator):
    bl_idname = 'mcbedrock.outlines'
    bl_label = 'Outlines'
    bl_description = 'Add, remove or refresh Sparkle outlines'
    bl_options = {'REGISTER', 'UNDO'}
    action: bpy.props.EnumProperty(items=(
        ('ADD_SELECTED', 'Add to selection', 'Outline the selected models, rigs and objects'),
        ('ADD_WORLDS', 'Add to imported worlds', 'Outline every imported MiEx world in this scene'),
        ('REMOVE_SELECTED', 'Remove from selection', 'Remove outlines from the selected objects'),
        ('REMOVE_ALL', 'Remove all', 'Remove outlines from everything in this scene'),
        ('REFRESH', 'Refresh', 'Recompute edges of the selected objects after editing their geometry'),
        ('RESET', 'Reset settings', 'The active model uses the scene settings again; with no model, '
                                    'every scene setting goes back to its default'),
    ), options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.mode == 'OBJECT'

    def execute(self, context):
        import time
        scene = context.scene
        started = time.perf_counter()
        plural = lambda count, word: f'{count} {word}' + ('' if count == 1 else 's')
        if self.action == 'ADD_SELECTED':
            # Picking blocks out of a world (trees) gives them model lines.
            count = add(scene, context.selected_objects, picked=context.selected_objects)
            if not count:
                found = expand(context.selected_objects)
                warning = ('Outlines follow square and rectangular faces, and the selection has none'
                           if found else 'Select a model, rig or block object first')
                if found:  # stays on the Outlines page, not just a passing popup
                    scene.mcbedrock_outlines_status = warning
                self.report({'WARNING'}, warning)
                return {'CANCELLED'}
            message = 'Outlines on ' + plural(count, 'object')
        elif self.action == 'ADD_WORLDS':
            worlds = world_collections(scene)
            if not worlds:
                self.report({'WARNING'}, 'Import a world with the World Importer first')
                return {'CANCELLED'}
            for collection in worlds:
                collection[WORLD] = True
            count = add(scene, [obj for c in worlds for obj in subtree_objects(c)], stroke=False)
            message = 'Outlines on ' + plural(len(worlds), 'world') + f' ({plural(count, "mesh")})'
        elif self.action == 'REMOVE_SELECTED':
            # Only what is selected, never everything.
            if not any(has_data(obj) for obj in expand(context.selected_objects)):
                self.report({'WARNING'}, 'Select objects that have outlines first')
                return {'CANCELLED'}
            count = remove(scene, context.selected_objects)
            message = 'Outlines removed from ' + plural(count, 'object')
        elif self.action == 'REMOVE_ALL':
            for collection in world_collections(scene):
                collection[WORLD] = False
            remove(scene)
            message = 'Outlines removed'
        elif self.action == 'REFRESH':
            count = refresh(scene, context.selected_objects)
            if not count:
                self.report({'WARNING'}, 'Select objects that have outlines first')
                return {'CANCELLED'}
            message = 'Edges recomputed for ' + plural(count, 'object')
        else:
            edited = models(context)
            if edited:
                for obj in edited:
                    if has_own(obj):
                        drop_own(obj)
                sync_stroke(scene)
                update(scene, force=True)
                message = context.view_layer.objects.active.name + ' uses the scene settings'
            else:
                reset(scene)
                message = 'Outline settings reset'
        if self.action not in ('RESET', 'REMOVE_ALL'):
            message += f' in {time.perf_counter() - started:.1f} s'
        scene.mcbedrock_outlines_status = message
        self.report({'INFO'}, message)
        return {'FINISHED'}


# ---------------------------------------------------------------- handlers

_signatures = {}


def _signature(scene, outlined=None):
    return tuple((obj.as_pointer(), tuple(s.material.as_pointer() if s.material else 0 for s in obj.material_slots))
                 for obj in (outlined_objects(scene) if outlined is None else outlined))


@persistent
def depsgraph_update(scene, depsgraph=None):
    if not groups():
        return
    parts = scene_parts(scene)
    if depsgraph is not None and (depsgraph.id_type_updated('MATERIAL') or depsgraph.id_type_updated('OBJECT')
                                  or depsgraph.id_type_updated('MESH')):
        if _signatures.get(scene.as_pointer()) != _signature(scene, parts[0]):
            sync(scene)
            parts = None  # sync can change which objects carry outlines
    update(scene, parts=parts)


@persistent
def frame_update(scene, *args):
    if groups():
        update(scene)


_view_keys = {}


def _poll_views():
    """Viewport zoom and lens change no data: watch them a few times a second."""
    # A timer that raises is unregistered for good: outlines would stop
    # following the view for the rest of the session.
    try:
        manager = bpy.context.window_manager
        if groups():
            for scene in {window.scene for window in manager.windows} if manager else ():
                if not hasattr(scene, 'mcbedrock_outlines') or scene.as_pointer() in _rendering:
                    continue
                key = view_scale(scene)
                key = tuple(round(v, 9) for v in key) if key else None
                if _view_keys.get(scene.as_pointer(), 0) != key:
                    _view_keys[scene.as_pointer()] = key
                    update(scene)
    except Exception as exc:
        if str(exc) not in _poll_errors:
            _poll_errors.add(str(exc))
            print(f'[Sparkle] Outlines view update failed: {exc}')
    return .25


_poll_errors = set()


@persistent
def render_done(scene, *args):
    if scene.as_pointer() in _rendering:
        _rendering.discard(scene.as_pointer())
        _view_keys.pop(scene.as_pointer(), None)       # back to the viewport


@persistent
def render_update(scene, *args):
    _rendering.add(scene.as_pointer())
    if any(has_data(o) for o in scene.objects):
        sync(scene)
        if stroke_wanted(scene):
            from . import outline_stroke
            outline_stroke.setup(scene)
        update(scene, force=True)


@persistent
def load_update(*args):
    _signatures.clear()
    remove_legacy_everywhere()
    lines = find_group(create=False)
    if lines is not None and lines.library is None:
        # Built for what this file uses: as many light slots as its busiest
        # scene's lights, and the per-model lookups only if a model has its own.
        scenes = [scene for scene in bpy.data.scenes if hasattr(scene, 'mcbedrock_outlines')]
        reshape(lines, lights=max((light_slots(scene) for scene in scenes), default=0),
                own=any(has_own(obj) for scene in scenes for obj in outlined_objects(scene) if lined(obj)),
                exact=True)
    for scene in bpy.data.scenes:
        if hasattr(scene, 'mcbedrock_outlines') and groups():
            upgrade(scene)
            # Models' own settings are packed again: the layout follows this version.
            for obj in outlined_objects(scene):
                if has_own(obj) and not obj.library:
                    write_own(obj)
            update(scene, force=True)


CLASSES = (OutlineSettings, MCBEDROCK_OT_outlines)
HANDLERS = ((bpy.app.handlers.depsgraph_update_post, depsgraph_update),
            (bpy.app.handlers.frame_change_post, frame_update),
            (bpy.app.handlers.render_pre, render_update),
            (bpy.app.handlers.render_post, render_done),
            (bpy.app.handlers.render_cancel, render_done),
            (bpy.app.handlers.load_post, load_update))


def register():
    unregister()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mcbedrock_outlines = bpy.props.PointerProperty(type=OutlineSettings)
    bpy.types.Scene.mcbedrock_outlines_status = bpy.props.StringProperty(options={'SKIP_SAVE'})
    # A model's own line settings (used once it has OWN_* properties, see has_own).
    bpy.types.Object.mcbedrock_outlines = bpy.props.PointerProperty(type=OutlineSettings)
    for handlers, callback in HANDLERS:
        handlers.append(callback)
    if not bpy.app.background and not bpy.app.timers.is_registered(_poll_views):
        bpy.app.timers.register(_poll_views, first_interval=.5, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_poll_views):
        bpy.app.timers.unregister(_poll_views)
    for handlers, callback in HANDLERS:
        for item in [h for h in handlers if getattr(h, '__name__', '') == callback.__name__
                     and getattr(h, '__module__', '') == callback.__module__]:
            handlers.remove(item)
    for name in ('mcbedrock_outlines', 'mcbedrock_outlines_status'):
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    if hasattr(bpy.types.Object, 'mcbedrock_outlines'):
        del bpy.types.Object.mcbedrock_outlines
    for cls in reversed(CLASSES):
        # The class itself first: a PropertyGroup is never bpy.types.<name>,
        # so a lookup alone left OutlineSettings registered and the add-on
        # could not be turned back on. The name finds an older module copy.
        registered = cls if cls.is_registered else getattr(bpy.types, cls.__name__, None)
        if registered is not None:
            try:
                bpy.utils.unregister_class(registered)
            except RuntimeError:
                pass
    _signatures.clear()
