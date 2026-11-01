"""Light blocks glow in Sparkle's scene materials.

MiEx makes a few full-block light sources emissive (glowstone, sea lantern,
lava, fire). Torches, lanterns and the other light blocks import as plain lit
textures, so at night their flames go dark and Sky & Lighting's light glow has
nothing to spread. Prepared copies of those materials emit their brightest
pixels: the flame, the lamp glass, the carved face. Originals are untouched.

The block list and light levels follow MiEx's block light table
(miex_lighting.json). The glowing pixels are found per texture, from its own
brightest value, so no hand-drawn masks are needed.
"""
import re

import bpy

MARKER = 'mc_light_block_version'
VERSION = 1
BRIGHTNESS = 'MC Light block brightness'
# Pixels this close to the texture's brightest value glow, fading in between.
FADE = (.72, .9)
# Large atlases are not single blocks; reading them would be slow.
MAX_PIXELS = 512 * 512

# Texture resource -> light level (0 to 15). Several blocks light up only in a
# lit state with its own texture (furnace_front_on, redstone_lamp_on, ...).
SOURCES = {
    'torch': 14, 'copper_torch': 14, 'soul_torch': 10, 'redstone_torch': 7,
    'lantern': 15, 'soul_lantern': 10, 'copper_lantern': 15, 'exposed_copper_lantern': 15,
    'weathered_copper_lantern': 15, 'oxidized_copper_lantern': 15,
    'jack_o_lantern': 15, 'end_rod': 14, 'beacon': 15, 'redstone_lamp_on': 15,
    'campfire_log_lit': 15, 'soul_campfire_log_lit': 10,
    'furnace_front_on': 13, 'blast_furnace_front_on': 13, 'smoker_front_on': 13,
    'copper_bulb_lit': 15, 'copper_bulb_lit_powered': 15,
    'exposed_copper_bulb_lit': 12, 'exposed_copper_bulb_lit_powered': 12,
    'weathered_copper_bulb_lit': 8, 'weathered_copper_bulb_lit_powered': 8,
    'oxidized_copper_bulb_lit': 4, 'oxidized_copper_bulb_lit_powered': 4,
    'cave_vines_lit': 14, 'cave_vines_plant_lit': 14, 'glow_lichen': 7,
    'crying_obsidian': 10, 'respawn_anchor_top': 15,
    'ochre_froglight_top': 15, 'ochre_froglight_side': 15,
    'verdant_froglight_top': 15, 'verdant_froglight_side': 15,
    'pearlescent_froglight_top': 15, 'pearlescent_froglight_side': 15,
    # MiEx already makes these emissive; they share the brightness control.
    'glowstone': 15, 'sea_lantern': 15, 'shroomlight': 15, 'magma': 3,
    'lava_still': 15, 'lava_flow': 15, 'fire_0': 15, 'fire_1': 15,
    'soul_fire_0': 10, 'soul_fire_1': 10, 'campfire_fire': 15, 'soul_campfire_fire': 10,
    'nether_portal': 11, 'amethyst_cluster': 5, 'large_amethyst_bud': 4,
    'medium_amethyst_bud': 2, 'small_amethyst_bud': 1,
    # Bedrock texture names, for worlds exported with Bedrock resource packs.
    'torch_on': 14, 'redstone_torch_on': 7, 'pumpkin_face_on': 15,
    'cave_vines_head_berries': 14, 'cave_vines_body_berries': 14,
}

_PREFIX = re.compile(r'^(?:mat_)?(?:minecraft[:_](?:blocks?[_/])?|(?:textures/)?blocks?/)?')
_HASH = re.compile(r'_[0-9a-f]{8,32}$')


def resource(name, *, texture=False):
    """'MAT_minecraft_block_torch_1a2b3c4d' or '1a2b3c4d_torch.png' -> 'torch'."""
    name = str(name).replace('\\', '/').lower()
    name = re.sub(r'\.\d{3,}$', '', name)
    if texture:
        name = name.rsplit('/', 1)[-1]
        name = re.sub(r'\.(?:png|tga|jpg|jpeg|exr)$', '', name)
        # Portable MiEx textures are prefixed with a content hash.
        name = re.sub(r'^[0-9a-f]{8,32}_', '', name)
    return _HASH.sub('', _PREFIX.sub('', name, count=1))


def _shader(material):
    shaders = [n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'] if material.node_tree else []
    return shaders[0] if len(shaders) == 1 else None


def _base_source(material, shader):
    """The texture behind Base Color, before Sparkle's surface finish layer."""
    from .lighting_pipeline import linked_from
    source = linked_from(shader.inputs['Base Color'])
    if source is None:
        return None
    node = source.node
    if node.type == 'GROUP' and node.name.startswith('MC Surface Finish'):
        return linked_from(node.inputs.get('Color'))
    return source


def _texture(socket):
    node = socket.node if socket is not None else None
    return node.image if node is not None and node.type == 'TEX_IMAGE' else None


def level(material):
    """Light level of a light block's material, else None."""
    if material is None or not material.node_tree:
        return None
    key = resource(material.get('mcaddon_miex_source', '') or material.name)
    if key in SOURCES:
        return SOURCES[key]
    shader = _shader(material)
    image = _texture(_base_source(material, shader)) if shader else None
    if image is None:
        return None
    for name in (image.name, image.filepath):
        if name and resource(name, texture=True) in SOURCES:
            return SOURCES[resource(name, texture=True)]
    return None


def _emissive(shader):
    strength, color = shader.inputs['Emission Strength'], shader.inputs['Emission Color']
    return ((strength.is_linked or strength.default_value > 0)
            and (color.is_linked or max(color.default_value[:3]) > 0))


def brightest(image):
    """Brightest channel of the opaque pixels, in linear color."""
    width, height = image.size
    if not width or width * height > MAX_PIXELS:
        return 1.
    try:
        import numpy as np
        pixels = np.empty(width * height * 4, np.float32)
        image.pixels.foreach_get(pixels)
    except (RuntimeError, ValueError):
        return 1.
    pixels = pixels.reshape(-1, 4)
    opaque = pixels[pixels[:, 3] > .5, :3] if image.alpha_mode != 'NONE' else pixels[:, :3]
    if not len(opaque):
        return 1.
    if not image.is_float and image.colorspace_settings.name.lower().startswith('srgb'):
        opaque = np.where(opaque <= .04045, opaque / 12.92, ((opaque + .055) / 1.055) ** 2.4)
    return float(max(opaque.max(), 1e-3))


def needs_rebuild(original, prepared):
    """Copies prepared before light blocks glowed lack the emission branch."""
    return level(original) is not None and prepared is not None and not prepared.get(MARKER)


def prepare_material(scene, material, original):
    """Give a prepared copy of a light block its emission, before its passes are built."""
    light = level(original)
    if light is None or material.get(MARKER):
        return False
    # Marked even when it cannot glow, so the copy is not rebuilt again.
    material[MARKER] = VERSION
    shader = _shader(material)
    if shader is None:
        return False
    nodes, links = material.node_tree.nodes, material.node_tree.links
    x, y = shader.location.x - 520, shader.location.y - 420
    brightness = nodes.new('ShaderNodeValue')
    brightness.name = BRIGHTNESS
    brightness.label = 'Light block brightness'
    brightness.location = (x, y - 260)
    scale = .5 + .5 * light / 15
    brightness.outputs[0].default_value = scale * _setting(scene)
    brightness['mc_light_level_scale'] = scale
    strength = shader.inputs['Emission Strength']
    if _emissive(shader):
        # MiEx's emissive blocks keep their emission; only its strength follows.
        if not strength.is_linked:
            amount = nodes.new('ShaderNodeMath')
            amount.operation = 'MULTIPLY'
            amount.name = 'MC Light block strength'
            amount.location = (x + 200, y - 260)
            amount.inputs[0].default_value = strength.default_value
            links.new(brightness.outputs[0], amount.inputs[1])
            links.new(amount.outputs[0], strength)
        return True
    source = _base_source(material, shader)
    if source is None:
        nodes.remove(brightness)
        return False
    image = _texture(source)
    peak = brightest(image) if image else max(shader.inputs['Base Color'].default_value[:3])
    channels = nodes.new('ShaderNodeSeparateColor')
    channels.location = (x, y)
    links.new(source, channels.inputs[0])
    value = nodes.new('ShaderNodeMath')
    value.operation = 'MAXIMUM'
    value.location = (x + 160, y)
    links.new(channels.outputs[0], value.inputs[0])
    links.new(channels.outputs[1], value.inputs[1])
    peak_value = nodes.new('ShaderNodeMath')
    peak_value.operation = 'MAXIMUM'
    peak_value.location = (x + 320, y)
    links.new(value.outputs[0], peak_value.inputs[0])
    links.new(channels.outputs[2], peak_value.inputs[1])
    mask = nodes.new('ShaderNodeMapRange')
    mask.name = 'MC Light block glowing pixels'
    mask.label = 'Glowing pixels (near the texture\'s brightest)'
    mask.interpolation_type = 'SMOOTHSTEP'
    mask.location = (x + 480, y)
    links.new(peak_value.outputs[0], mask.inputs['Value'])
    mask.inputs['From Min'].default_value = FADE[0] * peak
    mask.inputs['From Max'].default_value = FADE[1] * peak
    glow = nodes.new('ShaderNodeMix')
    glow.data_type = 'RGBA'
    glow.blend_type = 'MULTIPLY'
    glow.name = 'MC Light block emission'
    glow.location = (x + 660, y)
    glow.inputs['Factor'].default_value = 1
    links.new(source, glow.inputs['A'])
    links.new(mask.outputs['Result'], glow.inputs['B'])
    links.new(glow.outputs['Result'], shader.inputs['Emission Color'])
    links.new(brightness.outputs[0], strength)
    return True


def _setting(scene):
    s = scene.mcbedrock_sky
    return s.light_block_brightness if s.light_blocks else 0.


def update(scene):
    """Follow the brightness control on every prepared light block."""
    s = scene.mcbedrock_sky
    amount = _setting(scene)
    from .lighting_pipeline import set_value
    for material in {entry.prepared for entry in s.materials if entry.prepared}:
        node = material.node_tree.nodes.get(BRIGHTNESS) if material.get(MARKER) else None
        if node is not None:
            set_value(node.outputs[0], 'default_value', node.get('mc_light_level_scale', 1.) * amount)
    update_lights(scene)


# ---------------------------------------------------------------- block light
# Torches light the blocks around them, as in Element Animation's night shots:
# warm pockets of light against a dark world. One point light per lit block
# cell; colors stay a little short of saturated, as in the trailers.

LIGHT = 'mc_block_light'
SIGNATURE = 'mc_block_light_signature'
MAX_LIGHTS = 512
# A lava lake or a row of lanterns lights its surroundings about like one
# block does, so light blocks this close (in blocks, same color) share one
# light. One point light per lava block put over a thousand shadow-casting
# lights in a big world: EEVEE's shadow pool (about 4096 tiles, at least 6 per
# point light) overflowed and the sun's own shadows broke into soft blotches.
CLUSTER = 4.
MAX_SHADOWED = 96     # clusters nearest the camera that cast shadows
REACH = 16.           # a block light's reach in blocks, as in the game
LAYOUT = 4            # bump to re-place lights in saved scenes
GAIN = 'mc_cluster_gain'
# Point-light power, in watts, of a level 15 light at the default brightness.
POWER = 60.
WARM = (1., .68, .4)
TINTS = (
    ('soul', (.45, .85, 1.)), ('redstone', (1., .25, .12)), ('lava', (1., .48, .18)), ('magma', (1., .45, .15)),
    ('fire', (1., .6, .3)), ('campfire', (1., .62, .34)), ('sea_lantern', (.78, .92, 1.)), ('glowstone', (1., .8, .55)),
    ('shroomlight', (1., .7, .45)), ('end_rod', (1., .95, .9)), ('beacon', (.85, .95, 1.)),
    ('verdant_froglight', (.75, 1., .7)), ('pearlescent_froglight', (1., .85, 1.)), ('ochre_froglight', (1., .9, .6)),
    ('amethyst', (.75, .55, 1.)), ('crying_obsidian', (.6, .3, 1.)), ('nether_portal', (.6, .3, 1.)),
    ('glow_lichen', (.75, 1., .85)), ('cave_vines', (1., .78, .45)), ('copper', (1., .82, .62)),
)


# Warm light blocks glow at a color temperature rather than an RGB tint, so
# they read relative to the camera's white balance (Element sets each light
# as temperature against one white point; bramstout.nl, Unreal pipeline).
KELVIN = {'lava': 1700, 'magma': 1700, 'fire': 1900, 'campfire': 1900, 'shroomlight': 2400,
          'cave_vines': 2600, 'glowstone': 2700, 'copper': 3000}
KELVIN_BY_COLOR = {WARM: 2200, **{color: KELVIN[part] for part, color in TINTS if part in KELVIN}}


def tint(key):
    return next((color for part, color in TINTS if part in key), WARM)


def _key(material):
    key = resource(material.get('mcaddon_miex_source', '') or material.name)
    if key in SOURCES:
        return key
    shader = _shader(material)
    image = _texture(_base_source(material, shader)) if shader else None
    return resource(image.name, texture=True) if image else key


def enabled(scene):
    s = scene.mcbedrock_sky
    return s.light_blocks and s.block_lights and s.light_block_brightness > 0


def sources(scene):
    """One (location, level, color) per block cell holding a light block's faces."""
    import numpy as np
    from mathutils import Vector
    s = scene.mcbedrock_sky
    slots = {}
    for entry in s.materials:
        obj = entry.object
        if obj is None or obj.type != 'MESH' or entry.original is None or entry.prepared is None:
            continue
        light = level(entry.original)
        if light:
            slots.setdefault(obj, {})[entry.index] = (light, tint(_key(entry.original)))
    cells = {}
    for obj, lit in slots.items():
        mesh = obj.data
        count = len(mesh.polygons)
        if not count:
            continue
        index = np.empty(count, np.int32)
        mesh.polygons.foreach_get('material_index', index)
        chosen = np.flatnonzero(np.isin(index, list(lit)))
        if not len(chosen):
            continue
        centers = np.empty(count * 3, np.float32)
        mesh.polygons.foreach_get('center', centers)
        normals = np.empty(count * 3, np.float32)
        mesh.polygons.foreach_get('normal', normals)
        # World space, in blocks: a MiEx world imports Y-up and in model
        # pixels, so its meshes' own axes are turned and 16x too fine.
        matrix = np.array(obj.matrix_world, np.float64)
        turn = matrix[:3, :3]
        centers = centers.reshape(-1, 3)[chosen] @ turn.T + matrix[:3, 3]
        normals = normals.reshape(-1, 3)[chosen] @ np.linalg.inv(turn)
        normals /= np.maximum(np.linalg.norm(normals, axis=1), 1e-9)[:, None]
        # A face on a cell border belongs to the block it faces out of.
        inside = centers - normals * .01
        for slot, cell, center in zip(index[chosen], np.floor(inside).astype(np.int64), centers):
            item = cells.setdefault((obj.name, *cell), [[], 0, None, cell])
            item[0].append(center)
            if lit[slot][0] > item[1]:
                item[1], item[2] = lit[slot]
    locations, kinds, grid = [], [], {}
    for faces, light, color, cell in cells.values():
        # The light sits just above the cell's highest faces (a torch's
        # flame, at the tip of a leaning wall torch), outside the model, so
        # the block's own faces do not shadow it.
        faces = np.array(faces)
        top = faces[:, 2].max()
        tip = faces[faces[:, 2] > top - .1].mean(axis=0)
        location = Vector((tip[0], tip[1], min(top + .1, cell[2] + .95)))
        # A leaning wall torch or a lantern's chain can reach into the next
        # cell; pieces closer than most of a block are one light.
        key = tuple(int(v // .75) for v in location)
        near = next((i for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
                     for i in grid.get((key[0] + dx, key[1] + dy, key[2] + dz), ())
                     if (locations[i] - location).length < .75), None)
        if near is None:
            grid.setdefault(key, []).append(len(locations))
            locations.append(location)
            kinds.append((light, color))
        else:
            locations[near] = (locations[near] + location) / 2
            kinds[near] = max(kinds[near], (light, color), key=lambda kind: kind[0])
    return locations, kinds


def clusters(locations, kinds):
    """Light blocks merged on a CLUSTER-block grid, per color:
    [(location, level, color, gain, soft size)].

    A cluster is a little brighter than one block (the square root of its
    size, at most 4x) and its light as wide as the cluster, not the sum of
    its blocks: in the game a lava lake does not outshine one lava block.
    """
    import math
    from mathutils import Vector
    groups = {}
    for location, (light, color) in zip(locations, kinds):
        key = (tuple(math.floor(v / CLUSTER) for v in location), tuple(round(c, 3) for c in color))
        group = groups.get(key)
        if group is None:
            group = groups[key] = [Vector(), 0, 0, color, Vector(location), Vector(location)]
        group[0] += location
        group[1] += 1
        group[2] = max(group[2], light)
        group[4] = Vector(map(min, group[4], location))
        group[5] = Vector(map(max, group[5], location))
    found = []
    for total, count, light, color, low, high in groups.values():
        gain = round(min(count, 16) ** .5 * 2) / 2
        size = .12 if count == 1 else round(min(max((high - low).length / 2, .12), 1.5) * 4) / 4
        found.append((total / count, light, color, gain, size))
    return found


def _signature(scene):
    s = scene.mcbedrock_sky
    items = sorted((e.object.name, e.index, len(e.object.data.polygons), tuple(round(v, 4) for row in e.object.matrix_world for v in row))
                   for e in s.materials if e.object and e.object.type == 'MESH' and e.original and level(e.original))
    return str(hash((tuple(items), s.block_lights, s.light_blocks, LAYOUT)))


def owned_lights(scene):
    s = scene.mcbedrock_sky
    return [o for o in s.collection.objects if o.get(LIGHT)] if s.collection else []


def place_lights(scene):
    """Match the owned block lights to the light blocks in prepared materials."""
    from .sky import TAG
    s = scene.mcbedrock_sky
    if s.collection is None:
        return
    signature = _signature(scene) if enabled(scene) else ''
    if s.collection.get(SIGNATURE) == signature and (signature == '') == (not owned_lights(scene)):
        update_lights(scene)
        return
    for obj in owned_lights(scene):
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and not data.users:
            bpy.data.lights.remove(data)
    s.collection[SIGNATURE] = signature
    if not signature:
        return
    found = clusters(*sources(scene))
    # Nearest the camera first: only those keep their shadows, and past
    # MAX_LIGHTS the farthest go.
    eye = scene.camera.matrix_world.translation if scene.camera else scene.cursor.location
    found.sort(key=lambda cluster: (cluster[0] - eye).length_squared)
    found = found[:MAX_LIGHTS]
    shared = {}
    for rank, (location, light, color, gain, size) in enumerate(found):
        shadowed = rank < MAX_SHADOWED
        kind = (light, color, gain, size, shadowed)
        data = shared.get(kind)
        if data is None:
            data = shared[kind] = bpy.data.lights.new(f'MC Block light {light}', 'POINT')
            data[TAG], data[LIGHT], data[GAIN] = s.owner, light, gain
            kelvin = KELVIN_BY_COLOR.get(tuple(color))
            if kelvin and hasattr(data, 'use_temperature'):
                data.use_temperature = True
                data.temperature = kelvin
                data.color = (1., 1., 1.)
            else:
                data.color = color
            data.shadow_soft_size = size
            data.use_shadow = shadowed
            if hasattr(data, 'shadow_maximum_resolution'):
                data.shadow_maximum_resolution = .02
            if hasattr(data, 'use_custom_distance'):
                data.use_custom_distance = True
                data.cutoff_distance = REACH
            if hasattr(data, 'use_soft_falloff'):
                data.use_soft_falloff = True
        obj = bpy.data.objects.new('MC Block light', data)
        obj[TAG], obj[LIGHT], obj['role'] = s.owner, True, 'Block light'
        obj.location = location
        obj.hide_select = True
        s.collection.objects.link(obj)
    update_lights(scene)


def update_lights(scene):
    """Light power follows the light level and the brightness control."""
    amount = _setting(scene) / 2
    from .lighting_pipeline import set_value
    for data in {o.data for o in owned_lights(scene) if o.data}:
        light = data.get(LIGHT, 15)
        set_value(data, 'energy', POWER * (light / 15) ** 2 * amount * data.get(GAIN, 1.))
