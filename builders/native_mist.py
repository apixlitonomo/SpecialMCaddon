"""Native Mist distance with a separate, antialiased terrain coverage mask."""
import json


def coverage(scene, material):
    from .lighting_pipeline import linked_from, pass_name
    tree = material.node_tree
    if tree is None:
        return
    name = pass_name(scene, 'Terrain mask')
    node = next((n for n in tree.nodes if n.type == 'OUTPUT_AOV' and n.aov_name == name), None)
    if node is None:
        node = tree.nodes.new('ShaderNodeOutputAOV')
        node.name = 'MC Terrain mask'
        node.aov_name = name
    # Cycles can write an AOV before the transparent closure is skipped. A
    # constant one therefore marks even fully invisible cutout pixels as land.
    # Follow the material's actual alpha, including our optional opacity nodes.
    shaders = [n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED']
    alpha = shaders[0].inputs['Alpha'] if len(shaders) == 1 else None
    target = node.inputs['Value']
    source = linked_from(alpha)
    if source:
        if linked_from(target) != source:
            tree.links.new(source, target)
    else:
        if target.is_linked:
            for link in list(target.links): tree.links.remove(link)
        value = alpha.default_value if alpha else 1.
        if target.default_value != value: target.default_value = value


def setup(scene, tree, render):
    from .lighting_pipeline import Graph, TAG, pass_name
    s = scene.mcbedrock_sky
    # Keep a user's pre-existing Mist pass enabled when Lighting is restored.
    saved = json.loads(s.previous_mist_passes or '{}')
    for layer in scene.view_layers:
        saved.setdefault(layer.name, layer.use_pass_mist)
        layer.use_pass_mist = True
        layer.update_render_passes()
    s.previous_mist_passes = json.dumps(saved)
    factor = tree.nodes.get('MC Native mist strength')
    if factor is None:
        g = Graph(tree)
        masked = g.op('MULTIPLY', render.outputs['Mist'], render.outputs[pass_name(scene, 'Terrain mask')])
        factor = tree.nodes.new('ShaderNodeMath')
        factor.operation = 'MULTIPLY'
        factor.name = 'MC Native mist strength'
        factor[TAG] = s.owner
        tree.links.new(masked, factor.inputs[0])
    return factor.outputs[0]


def update(scene):
    from .lighting_pipeline import set_value, pass_name
    s = scene.mcbedrock_sky
    tree = s.compositor
    if tree is None or tree.nodes.get('MC Native mist strength') is None:
        return
    factor = tree.nodes['MC Native mist strength']
    set_value(factor.inputs[1], 'default_value', s.sun_world_haze)
    if s.sun_haze_mode == 'MIST' and s.world:
        mist = s.world.mist_settings
        set_value(mist, 'start', s.sun_world_haze_start)
        set_value(mist, 'depth', s.sun_world_haze_distance)
        set_value(mist, 'falloff', 'LINEAR')
        set_value(mist, 'height', 0.)
        source = factor.outputs[0]
    else:
        source = tree.nodes['MC Lighting passes'].outputs[pass_name(scene, 'Sun haze')]
    target = tree.nodes['MC Sun on terrain'].inputs[0]
    if not target.links or target.links[0].from_socket != source:
        tree.links.new(source, target)


def restore(scene):
    s = scene.mcbedrock_sky
    for name, enabled in json.loads(s.previous_mist_passes or '{}').items():
        layer = scene.view_layers.get(name)
        if layer:
            layer.use_pass_mist = enabled
    s.previous_mist_passes = ''
