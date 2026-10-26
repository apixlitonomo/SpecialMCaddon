"""Sun rays: god rays streaking from the visible sun through gaps in the scene.

The Minecraft trailers show light shafts falling through canopies and the sun
blooming over everything in front of it. This adds a Glare node in Sun Beams
mode after the sky glow: bright sky and the sun disk are smeared outward from
the sun's place on screen, so shafts appear wherever leaves, trunks or
terrain break the light. It is a compositor effect, cheap and identical in
EEVEE and Cycles, and it follows the camera and time of day.
"""
NODE = 'MC Sun rays'
GLOW = 'MC Sky glow'
THRESHOLD = 1.                          # only sky and sun brighter than this cast rays


def find(scene):
    tree = scene.mcbedrock_sky.compositor
    return tree.nodes.get(NODE) if tree else None


def ensure(scene):
    """The Sun Beams glare, chained after the sky glow; None without a compositor."""
    from .sky import TAG
    s = scene.mcbedrock_sky
    tree = s.compositor
    if tree is None:
        return None
    node = tree.nodes.get(NODE)
    if node is not None:
        return node
    glow = tree.nodes.get(GLOW)
    if glow is None:
        return None
    targets = [link.to_socket for link in glow.outputs['Image'].links]
    node = tree.nodes.new('CompositorNodeGlare')
    node.name = node.label = NODE
    node[TAG] = s.owner
    node.inputs['Type'].default_value = 'Sun Beams'
    node.inputs['Quality'].default_value = 'High'
    node.inputs['Threshold'].default_value = THRESHOLD
    node.location = (glow.location.x + 200, glow.location.y)
    tree.links.new(glow.outputs['Image'], node.inputs['Image'])
    for target in targets:
        tree.links.new(node.outputs['Image'], target)
    return node


def screen_position(scene, direction):
    """Where the sun sits in the frame (0..1 from the bottom left), or None behind the camera."""
    from bpy_extras.object_utils import world_to_camera_view
    camera = scene.camera
    if camera is None:
        return None
    far = camera.data.clip_end * .5 if camera.type == 'CAMERA' else 1000.
    point = camera.matrix_world.translation + direction.normalized() * far
    x, y, depth = world_to_camera_view(scene, camera, point)
    return (x, y) if depth > 0 else None


def update(scene):
    from .lighting_pipeline import set_value
    from .sky import time_state, current_time, smoothstep
    s = scene.mcbedrock_sky
    node = ensure(scene) if s.sun_rays > 0 else find(scene)
    if node is None:
        return
    direction, _, _ = time_state(current_time(scene), s.azimuth)
    position = screen_position(scene, direction)
    # Rays fade out as the sun sets below the horizon or leaves the frame far behind.
    up = smoothstep(-.06, .04, direction.z)
    strength = s.sun_rays * up if position is not None else 0.
    set_value(node, 'mute', strength <= 0)
    if position is not None:
        set_value(node.inputs['Sun Position'], 'default_value', position)
    set_value(node.inputs['Strength'], 'default_value', strength)
    set_value(node.inputs['Size'], 'default_value', s.sun_rays_length)
    sun = scene.mcbedrock_sky.collection and next(
        (o for o in s.collection.objects if o.get('role') == 'Sun'), None)
    tint = tuple(sun.data.color) if sun else (1., .9, .75)
    peak = max(max(tint), 1e-6)
    set_value(node.inputs['Tint'], 'default_value', (*(c / peak for c in tint), 1.))
