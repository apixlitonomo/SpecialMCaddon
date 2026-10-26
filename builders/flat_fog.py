"""Flat fog: a stylized depth fog laid over the render in the compositor.

Element Animation adds environment fog as a post-process from the sky's own
colors rather than as lit volumes (bramstout.nl, Element Unreal pipeline and
Element Animation pipeline). The result is flat and graphic: distance fades
evenly into the horizon color, with no shadows or light shafts inside it.

The fog reads the distance to each pixel, fades geometry beyond **Flat fog start**
toward the sky's horizon color (times **Flat fog tint**) and leaves the sky
itself untouched. In EEVEE the distance comes from the Mist pass, set
to a plain linear ramp over MIST_RANGE: unlike EEVEE's Depth it is averaged
over the samples, so silhouettes against fogged land and see-through water
get smooth fog instead of a dotted edge or a pale fringe. Cycles is the
other way round (its Mist speckles through water, its Depth is clean), so it
reads Depth, as do legacy Native Mist looks, which own the mist. The sky is found by its normal, which is zero there: its
depth is endless in a render but only the clip distance in the viewport
compositor, where a depth test fogged the whole sky. Clouds are part of the sky too: their geometry has a depth,
so an object Cryptomatte keeps the fog off them. EEVEE's Depth pass is not
filtered: under see-through water each pixel holds the surface or the floor,
so the fog amount is smoothed with an edge-aware blur guided by log depth:
the surface and the floor are close in depth ratio and blend, while a
silhouette against far land is a jump and stays sharp. It starts where Distance fog
starts, so the foreground stays crisp. It sits before the LOOCS view
transform, in scene-linear light, so it tone maps with the rest of the image.
"""
NODE = 'MC Flat fog'
CLOUD_MASK = NODE + ' clouds'
SMOOTH = NODE + ' smooth'
SKY = NODE + ' sky mask'
DISTANCE = NODE + ' distance in'
REQUIRED = (NODE, CLOUD_MASK, SMOOTH, SKY, DISTANCE)   # an older fog without one of these is rebuilt
ENABLED_MIST = 'mc_flat_fog_mist'     # view layer: Mist pass enabled by us
MIST_RANGE = 4000.                      # blocks the world's linear mist spans: Mist x range = distance
ENABLED_N = 'mc_flat_fog_normal'      # view layer: Normal pass enabled by us
ENABLED_Z = 'mc_flat_fog_z'             # view layer: Depth pass enabled by us
ENABLED_CRYPTO = 'mc_flat_fog_crypto'   # view layer: object Cryptomatte enabled by us
SKY_DEPTH = 1e4                         # depth beyond this is sky, not geometry
DEPTH_RATIO = .25                       # log-depth scale: under about 1.5x apart, depths blend


def _nodes(tree):
    return {name: tree.nodes.get(NODE + suffix) for name, suffix in
            (('mix', ''), ('amount', ' amount'), ('start', ' start'), ('distance', ' distance'), ('color', ' color'))}


def ensure_depth(scene):
    for layer in scene.view_layers:
        if not layer.use_pass_z:
            layer.use_pass_z = True
            layer[ENABLED_Z] = True
        if not layer.use_pass_normal:
            layer.use_pass_normal = True
            layer[ENABLED_N] = True
        if not layer.use_pass_mist:
            layer.use_pass_mist = True
            layer[ENABLED_MIST] = True


def clouds(scene):
    s = scene.mcbedrock_sky
    # The rig links its clouds into its own collection; reading that avoids a
    # walk over every object in a large world on each lighting update.
    objects = s.collection.all_objects if s.collection else scene.objects
    return sorted(o.name for o in objects if o.get('role') == 'Cloud' and o.get('mc_sky_owner') == s.owner)


def ensure_cloud_mask(scene):
    for layer in scene.view_layers:
        if not layer.use_pass_cryptomatte_object:
            layer.use_pass_cryptomatte_object = True
            layer[ENABLED_CRYPTO] = True


FOG_NODES = {'ShaderNodeMath', 'ShaderNodeValue', 'ShaderNodeVectorMath', 'CompositorNodeCryptomatteV2', 'CompositorNodeBilateralblur'}


def _take_out(tree, mix):
    """Remove an older flat fog, joining what fed it to what it fed; returns
    the socket it fed, where the new fog goes."""
    source = mix.inputs[6].links[0].from_socket if mix.inputs[6].links else None
    targets = [link.to_socket for link in mix.outputs[2].links]
    found, stack = set(), [link.from_node for link in mix.inputs[0].links]
    while stack:
        node = stack.pop()
        if node in found or node.bl_idname not in FOG_NODES:
            continue
        found.add(node)
        stack += [link.from_node for socket in node.inputs for link in socket.links]
    found |= {link.from_node for link in mix.inputs[7].links}
    for node in found | {mix}:
        tree.nodes.remove(node)
    if source is not None:
        for target in targets:
            tree.links.new(source, target)
    return targets[0] if targets and source is not None else None


def setup(scene, tree, render, destination):
    """Insert the fog on the link into destination (the LOOCS node's Image)."""
    from .lighting_pipeline import Graph, TAG
    existing = tree.nodes.get(NODE)
    if existing is not None:
        if all(tree.nodes.get(name) is not None for name in REQUIRED):
            return
        destination = _take_out(tree, existing) or destination     # an older fog: rebuild it in place
    if not destination.links:
        return
    ensure_depth(scene)
    owner = scene.mcbedrock_sky.owner
    source = destination.links[0].from_socket
    before = set(tree.nodes)
    g = Graph(tree)
    values = {}
    for suffix, default in ((' amount', .25), (' start', 48.), (' distance', 120.)):
        node = tree.nodes.new('ShaderNodeValue')
        node.name = node.label = NODE + suffix
        node.outputs[0].default_value = default
        values[suffix] = node.outputs[0]
    color = tree.nodes.new('CompositorNodeRGB')
    color.name = color.label = NODE + ' color'
    # The fog's distance: a relay update() feeds from Mist (smooth) or Depth.
    relay = tree.nodes.new('ShaderNodeMath')
    relay.name = relay.label = DISTANCE
    relay.operation = 'MULTIPLY'
    relay.inputs[1].default_value = 1.
    tree.links.new(render.outputs['Depth'], relay.inputs[0])
    depth = relay.outputs[0]
    # 1 - exp(-(depth - start) / distance): even, flat build-up with distance.
    beyond = g.op('MAXIMUM', g.op('SUBTRACT', depth, values[' start']), 0)
    thickness = g.op('DIVIDE', beyond, g.op('MAXIMUM', values[' distance'], 1e-3))
    fog = g.op('SUBTRACT', 1, g.op('EXPONENT', g.op('MULTIPLY', thickness, -1)))
    # Land has a unit normal, the sky none; antialiased edges fall between.
    length = tree.nodes.new('ShaderNodeVectorMath')
    length.name = length.label = SKY
    length.operation = 'LENGTH'
    tree.links.new(render.outputs['Normal'], length.inputs[0])
    land = g.op('MINIMUM', g.op('MULTIPLY', length.outputs['Value'], 1.5), 1)
    geometry = g.op('MULTIPLY', land, g.op('LESS_THAN', depth, SKY_DEPTH))
    factor = g.op('MULTIPLY', g.op('MULTIPLY', fog, values[' amount']), geometry)
    mask = tree.nodes.new('CompositorNodeCryptomatteV2')
    mask.name = mask.label = CLOUD_MASK
    mask.source = 'RENDER'
    mask.scene = scene
    mask.layer_name = scene.view_layers[0].name + '.CryptoObject'
    tree.links.new(render.outputs['Image'], mask.inputs['Image'])
    factor = g.op('MULTIPLY', factor, g.op('SUBTRACT', 1, mask.outputs['Matte']))
    smooth = tree.nodes.new('CompositorNodeBilateralblur')
    smooth.name = smooth.label = SMOOTH
    smooth.inputs['Size'].default_value = 3
    smooth.inputs['Threshold'].default_value = .1
    tree.links.new(factor, smooth.inputs['Image'])
    # Guided by depth, not color: dark silhouettes and dark land look alike.
    log_depth = g.op('MULTIPLY', g.op('LOGARITHM', g.op('MAXIMUM', depth, 1e-3), 2.718281828), DEPTH_RATIO)
    tree.links.new(log_depth, smooth.inputs['Determinator'])
    factor = smooth.outputs['Image']
    mix = tree.nodes.new('ShaderNodeMix')
    mix.name = NODE
    mix.data_type = 'RGBA'
    mix.clamp_result = False
    tree.links.new(factor, mix.inputs[0])
    tree.links.new(source, mix.inputs[6])
    tree.links.new(color.outputs[0], mix.inputs[7])
    tree.links.new(mix.outputs[2], destination)
    for node in set(tree.nodes) - before:
        node[TAG] = owner
    update(scene)


def _distance_source(scene, tree):
    """Feed the fog a distance: EEVEE's Mist (averaged over samples, so
    silhouettes and water stay smooth), but Cycles' Depth, because Cycles'
    Mist is noisy through see-through water (the denoiser never sees it)
    while its Depth stays clean. Depth too while a legacy Native Mist look
    owns the world's mist settings."""
    from .lighting_pipeline import set_value
    s = scene.mcbedrock_sky
    relay = tree.nodes.get(DISTANCE)
    render = tree.nodes.get('MC Lighting passes')
    if relay is None or render is None:
        return
    legacy = tree.nodes.get('MC Native mist strength') is not None and s.sun_haze_mode == 'MIST'
    mist = render.outputs.get('Mist')
    if legacy or mist is None or not mist.enabled or s.world is None or scene.render.engine == 'CYCLES':
        source, scale = render.outputs.get('Depth'), 1.
        if source is None:
            return          # off (amount 0) and no Depth pass asked for: nothing to measure
    else:
        settings = s.world.mist_settings
        set_value(settings, 'start', 0.)
        set_value(settings, 'depth', MIST_RANGE)
        set_value(settings, 'falloff', 'LINEAR')
        set_value(settings, 'height', 0.)
        source, scale = mist, MIST_RANGE
    if not relay.inputs[0].links or relay.inputs[0].links[0].from_socket != source:
        tree.links.new(source, relay.inputs[0])
    set_value(relay.inputs[1], 'default_value', scale)


def fog_color(scene):
    """The sky's horizon in scene-linear light, so distance melts into it."""
    s = scene.mcbedrock_sky
    nodes = s.world.node_tree.nodes if s.world and s.world.node_tree else None
    if nodes is None or nodes.get('Sky colors') is None or nodes.get('Visible sky') is None:
        base = (.6, .72, .85)
    else:
        from . import sky_drive
        horizon = nodes['Sky colors'].color_ramp.elements[1].color[:3]
        strength = sky_drive.value(nodes['Visible sky'].inputs['Strength'])
        base = tuple(h * strength for h in horizon)
    return tuple(b * t for b, t in zip(base, s.flat_fog_color))


def update(scene):
    from .lighting_pipeline import set_value
    s = scene.mcbedrock_sky
    tree = s.compositor
    if tree is None:
        return
    nodes = _nodes(tree)
    if any(node is None for node in nodes.values()):
        return
    names = clouds(scene)
    if s.flat_fog > 0:
        ensure_depth(scene)
        if names:
            ensure_cloud_mask(scene)
    mask = tree.nodes.get(CLOUD_MASK)
    if mask is not None and mask.matte_id != ','.join(names):
        mask.matte_id = ','.join(names)
    _distance_source(scene, tree)
    set_value(nodes['amount'].outputs[0], 'default_value', float(s.flat_fog))
    set_value(nodes['start'].outputs[0], 'default_value', float(s.flat_fog_start))
    set_value(nodes['distance'].outputs[0], 'default_value', float(s.flat_fog_distance))
    # Follows the sky every frame of a moving cycle: driven (sky_drive).
    from . import sky_drive
    sky_drive.write(nodes['color'].outputs[0], 'default_value', (*fog_color(scene), 1), tolerance=1e-6)


def restore(scene):
    """Give back the Depth and Cryptomatte passes if this module turned them on."""
    for layer in scene.view_layers:
        if layer.get(ENABLED_Z):
            layer.use_pass_z = False
            del layer[ENABLED_Z]
        if layer.get(ENABLED_MIST):
            layer.use_pass_mist = False
            del layer[ENABLED_MIST]
        if layer.get(ENABLED_N):
            layer.use_pass_normal = False
            del layer[ENABLED_N]
        if layer.get(ENABLED_CRYPTO):
            layer.use_pass_cryptomatte_object = False
            del layer[ENABLED_CRYPTO]
