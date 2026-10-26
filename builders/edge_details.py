"""Native MiEx corner shading and optional screen-space edge highlights."""
from pathlib import Path
import re

MARKER = 'mc_edge_detail'
PASSES = (('Edge normals', 'COLOR'), ('Edge coverage', 'VALUE'))
CORNER = 'mc_miex_corner_finish'
CORNER_UV = 'uvCornerST'


def has_corner_uvs(obj):
    layer = obj.data.uv_layers.get(CORNER_UV)
    return layer is not None and len(layer.data) == len(obj.data.loops) and len(layer.data) > 0


def corner_eligible(material):
    """Only unanimated, opaque imported terrain without an existing normal map."""
    if not material.get('mcaddon_miex_source') or not material.use_nodes or material.get('mc_edge_exclude'):
        return False
    tree = material.node_tree
    if tree.animation_data:
        return False
    names = (material.name + ' ' + str(material['mcaddon_miex_source'])).lower()
    if re.search(r'leaves|leaf|short_grass|tall_grass|fern|flower|vine|water|glass|ice|petal|sapling|bush|lily|poppy|tulip|orchid|allium|dandelion', names):
        return False
    shaders = [n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED']
    if len(shaders) != 1:
        return False
    ports = shaders[0].inputs
    return (not ports['Normal'].is_linked and not ports['Alpha'].is_linked and ports['Alpha'].default_value >= 1
            and not any(ports[k].is_linked or ports[k].default_value > 0
                        for k in ('Metallic', 'Transmission Weight')))


def corner_image(scene):
    import bpy
    owner = scene.mcbedrock_sky.owner
    for image in bpy.data.images:
        if image.get('mc_sky_owner') == owner and image.get(CORNER):
            return image
    path = Path(__file__).resolve().parents[1] / 'assets/miex/corner_atlas.png'
    image = bpy.data.images.load(str(path), check_existing=False)
    image.name = 'MC MiEx corner atlas'
    image.colorspace_settings.name = 'Non-Color'
    image.pack()
    image['mc_sky_owner'], image[CORNER] = owner, True
    return image


def prepare_corners(scene, material):
    """Use the authored atlas/second UV set; geometry and original UVs stay intact."""
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    uv = nodes.new('ShaderNodeUVMap')
    uv.name, uv.uv_map = 'MC Corner UV', CORNER_UV
    texture = nodes.new('ShaderNodeTexImage')
    texture.name = 'MC Corner atlas'
    texture.image = corner_image(scene)
    texture.interpolation, texture.extension = 'Closest', 'EXTEND'
    links.new(uv.outputs['UV'], texture.inputs['Vector'])
    normal = nodes.new('ShaderNodeNormalMap')
    # The optimizer can mirror the primary st layer independently. Using the
    # atlas basis keeps exposed-edge normals pointing outward on those faces.
    normal.name, normal.uv_map = 'MC Corner normal', CORNER_UV
    links.new(texture.outputs['Color'], normal.inputs['Color'])
    shader = next(n for n in nodes if n.type == 'BSDF_PRINCIPLED')
    links.new(normal.outputs['Normal'], shader.inputs['Normal'])
    # Blue is encoded tangent-normal Z: neutral interiors are 1, edge slopes
    # fall below 1. Derive a bounded mask, retaining the existing texture/tint.
    separate = nodes.new('ShaderNodeSeparateColor')
    links.new(texture.outputs['Color'], separate.inputs['Color'])
    mask = nodes.new('ShaderNodeMath')
    mask.operation = 'SUBTRACT'
    mask.inputs[0].default_value = 1
    links.new(separate.outputs['Blue'], mask.inputs[1])
    gain = nodes.new('ShaderNodeMath')
    gain.name, gain.operation = 'MC Corner albedo gain', 'MULTIPLY_ADD'
    gain.inputs[2].default_value = 1
    links.new(mask.outputs[0], gain.inputs[0])
    albedo = nodes.new('ShaderNodeMixRGB')
    albedo.name, albedo.blend_type = 'MC Corner albedo', 'MULTIPLY'
    albedo.inputs[0].default_value = 1
    port = nodes['MC Surface Finish'].inputs['Color']
    if port.links:
        links.new(port.links[0].from_socket, albedo.inputs[1])
    else:
        albedo.inputs[1].default_value = port.default_value
    links.new(gain.outputs[0], albedo.inputs[2])
    links.new(albedo.outputs[0], port)
    material[CORNER] = 1
    update_corners(scene, material)


def update_corners(scene, material):
    from .lighting_pipeline import set_value
    if not material.get(CORNER):
        return
    normal = material.node_tree.nodes.get('MC Corner normal')
    gain = material.node_tree.nodes.get('MC Corner albedo gain')
    if normal is None or gain is None:
        return  # Preserve a user's edited/deleted shader nodes during handlers.
    s = scene.mcbedrock_sky
    # MiEx corner shading created visible terrain outlines in imported worlds.
    # Keep the compatibility nodes inert; the user never requested outlines.
    set_value(normal.inputs['Strength'], 'default_value', 0)
    set_value(gain.inputs[1], 'default_value', 0)


def eligible(material):
    if material.get('mc_edge_exclude') or material.node_tree.animation_data:
        return False
    names = (material.name + ' ' + ' '.join(n.image.name for n in material.node_tree.nodes
             if n.type == 'TEX_IMAGE' and n.image)).lower()
    if re.search(r'leaves|leaf|grass|fern|flower|vine|water|glass|ice|petal|sapling|bush|moss|lily|poppy|tulip|orchid|allium|dandelion', names):
        return False
    shaders = [n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED']
    if len(shaders) != 1: return False
    return not any(shaders[0].inputs[k].is_linked or shaders[0].inputs[k].default_value > 0
                   for k in ('Metallic', 'Transmission Weight'))


def prepare_material(scene, material):
    from .lighting_pipeline import Graph, pass_name
    tree = material.node_tree
    if scene.mcbedrock_sky.edge_highlights <= 0 or not eligible(material):
        for node in list(tree.nodes):
            if node.get(MARKER): tree.nodes.remove(node)
        return
    if any(n.get(MARKER) for n in tree.nodes): return
    old = set(tree.nodes)
    g = Graph(tree)
    geometry = tree.nodes.new('ShaderNodeNewGeometry')
    separate = tree.nodes.new('ShaderNodeSeparateXYZ')
    tree.links.new(geometry.outputs['True Normal'], separate.inputs[0])
    alpha = next(n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED').inputs['Alpha']
    coverage = alpha.links[0].from_socket if alpha.links else alpha.default_value
    # Coverage is also used as a conservative mask on antialiased cutout edges.
    normal = g.combine([g.op('MULTIPLY', g.op('ADD', g.op('MULTIPLY', c, .5), .5), coverage)
                        for c in separate.outputs])
    for suffix, kind, value in (('Edge normals', 'Color', normal), ('Edge coverage', 'Value', coverage)):
        output = tree.nodes.new('ShaderNodeOutputAOV')
        output.aov_name = pass_name(scene, suffix)
        g.put(output.inputs[kind], value)
    for node in set(tree.nodes) - old: node[MARKER] = True


def setup(scene, tree, render, source):
    from .lighting_pipeline import Graph, group, socket, pass_name, TAG
    s = scene.mcbedrock_sky
    node = tree.nodes.get('MC Edge highlights')
    if s.edge_highlights <= 0:
        if node: node.mute = True
        return
    if node is None:
        effect = group(scene, 'Edge highlights', 'CompositorNodeTree')
        if not effect.nodes:
            for name in ('Image', 'Normals'): socket(effect, name)
            socket(effect, 'Coverage', kind='NodeSocketFloat')
            socket(effect, 'Amount', kind='NodeSocketFloat', default=0)
            socket(effect, 'Image', 'OUTPUT')
            inp = effect.nodes.new('NodeGroupInput')
            out = effect.nodes.new('NodeGroupOutput')
            g = Graph(effect)
            normal = g.combine([g.op('DIVIDE', c, g.op('MAXIMUM', inp.outputs['Coverage'], 1e-5))
                                for c in g.split(inp.outputs['Normals'])])
            kernel = effect.nodes.new('CompositorNodeFilter')
            kernel.inputs['Type'].default_value = 'Sobel'
            effect.links.new(normal, kernel.inputs['Image'])
            rgb = g.split(kernel.outputs['Image'])
            edge = g.op('MINIMUM', 1, g.op('MAXIMUM', rgb[0], g.op('MAXIMUM', rgb[1], rgb[2])))
            factor = g.op('ADD', 1, g.op('MULTIPLY', inp.outputs['Amount'], g.op('MULTIPLY', edge, inp.outputs['Coverage'])))
            rgba = effect.nodes.new('CompositorNodeSeparateColor')
            effect.links.new(inp.outputs['Image'], rgba.inputs[0])
            result = g.combine([g.op('MULTIPLY', c, factor) for c in list(rgba.outputs)[:3]], rgba.outputs[3])
            effect.links.new(result, out.inputs[0])
            effect['version'] = 1
        targets = [link.to_socket for link in source.links]
        node = tree.nodes.new('CompositorNodeGroup')
        node.name = 'MC Edge highlights'; node[TAG] = s.owner; node.node_tree = effect
        tree.links.new(source, node.inputs['Image'])
        for target in targets: tree.links.new(node.outputs[0], target)
    for suffix, port in (('Edge normals', 'Normals'), ('Edge coverage', 'Coverage')):
        output = render.outputs.get(pass_name(scene, suffix))
        if output and (not node.inputs[port].links or node.inputs[port].links[0].from_socket != output):
            tree.links.new(output, node.inputs[port])
    update(scene)


def update(scene):
    from .lighting_pipeline import set_value
    s = scene.mcbedrock_sky
    node = s.compositor.nodes.get('MC Edge highlights') if s.compositor else None
    if node:
        set_value(node, 'mute', s.edge_highlights <= 0)
        # Bounded to a modest 35% lift, leaving base color and alpha intact.
        set_value(node.inputs['Amount'], 'default_value', .35*s.edge_highlights)
