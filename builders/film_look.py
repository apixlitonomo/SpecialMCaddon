"""Film look: halation, world-locked grain and gate weave, in the compositor.

Three touches from Element Animation's films (bramstout.nl: A Warden's Song,
The Monster in the Woods, Adventure Awaits), on by default and kept subtle:

- Halation. Film glows around bright lights, more in red than in green or
  blue, which warms the light against a cool image. It is fed only by the
  emission pass (torches, lava, lanterns), never the sky, so daylight shots
  gain no bloom.
- Grain locked to the world. Noise read from each pixel's world position
  sticks to surfaces as the camera moves: it does not crawl over the image,
  and it survives video compression better than grain that changes every
  frame. The sky gets none.
- Gate weave. A film frame never sits perfectly still in the gate; a
  fraction of a pixel of slow drift per frame, driven by the scene frame.

It sits before the LOOCS view transform, in scene-linear light, after flat
fog. One switch, Film look (off by default), turns all three on; the
constants below set how strong they are.
"""
NODE = 'MC Film'
ENABLED_POSITION = 'mc_film_position'   # view layer: Position pass enabled by us
HALATION = .45                          # strength of the red-weighted glow
HALATION_TINT = (1., .38, .16)          # red spreads farthest in film
HALATION_SIZE = .7                      # glare size, 0-1
GRAIN = .035                            # +-3.5% brightness at most
GRAIN_CELLS = 48                        # grain cells per block
GATE_WEAVE = .18                        # pixels of drift at most
SKY_DEPTH = 1e4


def ensure_position(scene):
    for layer in scene.view_layers:
        if not layer.use_pass_position:
            layer.use_pass_position = True
            layer[ENABLED_POSITION] = True
        if not layer.use_pass_z:
            layer.use_pass_z = True


def _driver(socket, expression, scene):
    driver = socket.driver_add('default_value').driver
    driver.type = 'SCRIPTED'
    driver.expression = expression
    variable = driver.variables.new()
    variable.name = 'on'
    variable.type = 'SINGLE_PROP'
    variable.targets[0].id_type = 'SCENE'
    variable.targets[0].id = scene
    variable.targets[0].data_path = 'mcbedrock_sky.film_look'


def setup(scene, tree, render, destination):
    """Insert the film stage on the link into destination (the LOOCS node's Image)."""
    from .lighting_pipeline import Graph, TAG, pass_name
    if tree.nodes.get(NODE + ' halation') is not None or not destination.links:
        return
    ensure_position(scene)
    owner = scene.mcbedrock_sky.owner
    source = destination.links[0].from_socket
    before = set(tree.nodes)
    g = Graph(tree)
    image = source
    emission = render.outputs.get(pass_name(scene, 'Emission'))
    if emission is not None:
        glare = tree.nodes.new('CompositorNodeGlare')
        glare.name = glare.label = NODE + ' halation glow'
        if hasattr(glare, 'glare_type'):
            glare.glare_type = 'FOG_GLOW'
        type_socket = glare.inputs.get('Type')
        if type_socket is not None:
            type_socket.default_value = 'Fog Glow'
        if glare.inputs.get('Threshold') is not None:
            glare.inputs['Threshold'].default_value = 0.
        if glare.inputs.get('Size') is not None:
            glare.inputs['Size'].default_value = HALATION_SIZE
        tree.links.new(emission, glare.inputs['Image'])
        glow = glare.outputs.get('Glare') or glare.outputs[0]
        tint = tree.nodes.new('ShaderNodeMix')
        tint.name = tint.label = NODE + ' halation tint'
        tint.data_type = 'RGBA'
        tint.blend_type = 'MULTIPLY'
        tint.inputs[0].default_value = 1.
        tree.links.new(glow, tint.inputs[6])
        tint.inputs[7].default_value = (*(c * HALATION for c in HALATION_TINT), 1)
        add = tree.nodes.new('ShaderNodeMix')
        add.name = add.label = NODE + ' halation'
        add.data_type = 'RGBA'
        add.blend_type = 'ADD'
        add.clamp_result = False
        add.inputs[0].default_value = 0.
        tree.links.new(image, add.inputs[6])
        tree.links.new(tint.outputs[2], add.inputs[7])
        image = add.outputs[2]
    else:
        marker = tree.nodes.new('ShaderNodeValue')
        marker.name = marker.label = NODE + ' halation'
    # World-locked grain: white noise of the quantized world position.
    cells = tree.nodes.new('ShaderNodeVectorMath')
    cells.operation = 'SCALE'
    tree.links.new(render.outputs['Position'], cells.inputs[0])
    cells.inputs['Scale'].default_value = GRAIN_CELLS
    snapped = tree.nodes.new('ShaderNodeVectorMath')
    snapped.operation = 'FLOOR'
    tree.links.new(cells.outputs[0], snapped.inputs[0])
    noise = tree.nodes.new('ShaderNodeTexWhiteNoise')
    noise.name = noise.label = NODE + ' grain'
    noise.noise_dimensions = '3D'
    tree.links.new(snapped.outputs[0], noise.inputs['Vector'])
    on_geometry = g.op('LESS_THAN', render.outputs['Depth'], SKY_DEPTH)
    switch = tree.nodes.new('ShaderNodeValue')
    switch.name = switch.label = NODE + ' on'
    switch.outputs[0].default_value = 0.
    amount = g.op('MULTIPLY', g.op('MULTIPLY', g.op('MULTIPLY', g.op('SUBTRACT', noise.outputs['Value'], .5), 2 * GRAIN), on_geometry),
                  switch.outputs[0])
    gain = g.op('ADD', 1, amount)
    alpha = tree.nodes.new('CompositorNodeSeparateColor')
    tree.links.new(image, alpha.inputs[0])
    grained = g.combine([g.op('MULTIPLY', c, gain) for c in g.split(image)], alpha.outputs[3])
    # Gate weave: slow drift of a fraction of a pixel, from the frame number.
    weave = tree.nodes.new('CompositorNodeTranslate')
    weave.name = weave.label = NODE + ' gate weave'
    tree.links.new(grained, weave.inputs['Image'])
    _driver(weave.inputs['X'], f'on * {GATE_WEAVE} * (sin(frame * 0.61) * 0.6 + sin(frame * 1.73) * 0.4)', scene)
    _driver(weave.inputs['Y'], f'on * {GATE_WEAVE} * (sin(frame * 0.47 + 1.3) * 0.6 + sin(frame * 1.29 + 0.4) * 0.4)', scene)
    tree.links.new(weave.outputs[0], destination)
    for node in set(tree.nodes) - before:
        node[TAG] = owner


def update(scene):
    """Switch the stage with Film look, and keep the passes it reads."""
    from .lighting_pipeline import set_value
    s = scene.mcbedrock_sky
    tree = s.compositor
    if tree is None or tree.nodes.get(NODE + ' halation') is None:
        return
    on = float(bool(getattr(s, 'film_look', False)))
    halation = tree.nodes[NODE + ' halation']
    if halation.type == 'MIX':
        set_value(halation.inputs[0], 'default_value', on)
    # Off, the halation adds nothing (factor 0) but its glare still ran on every
    # frame: most of the viewport compositor's cost. Muted, it is skipped.
    glare = tree.nodes.get(NODE + ' halation glow')
    if glare is not None:
        set_value(glare, 'mute', not on)
    switch = tree.nodes.get(NODE + ' on')
    if switch is not None:
        set_value(switch.outputs[0], 'default_value', on)
    if on:
        ensure_position(scene)


def restore(scene):
    for layer in scene.view_layers:
        if layer.get(ENABLED_POSITION):
            layer.use_pass_position = False
            del layer[ENABLED_POSITION]
