"""Opt-in, reversible water looks for a single MiEx import.

These are Blender adaptations of Dungeons II and Vibrant Visuals, not ports of
either renderer. Dungeons uses authorized extracted flow/pixel masks; see its asset
provenance. Waves change shading normals only; imported geometry stays intact.
"""
import json
from pathlib import Path
import re

import bpy

from . import water_body, water_contact, water_depth
from ..exporters.miex.world_scale import subtree_objects

TAG = 'mcaddon_water_preset'
# Dungeons II pattern cells per block and reflection weight, matched to the
# user's October 4 gameplay recording.
DUNGEONS_CELLS = 4
DUNGEONS_REFLECTION = .03
STATE = 'mcaddon_water_slots'
SOURCE = 'mcaddon_miex_source'
VIBRANT_VERSION = 4
# Version 7 is version 5's graph again, so version 6 copies rebuild to it.
DUNGEONS_VERSION = 8
PLAIN_VERSION = 4           # 4: flat color, no sky reflection (3 reflected the sky)
VERSION_TAG = 'mcaddon_water_shader_version'
VERSIONS = {'VIBRANT': VIBRANT_VERSION, 'DUNGEONS': DUNGEONS_VERSION, 'PLAIN': PLAIN_VERSION}
PRESET_ITEMS = (
    ('ORIGINAL', 'Original water', 'Keep the imported resource-pack water'),
    ('DUNGEONS', 'MC Dungeons II', 'Blue-teal water with animated pixel reflections, refraction and a white contact outline'),
    ('VIBRANT', 'Vibrant Visuals', 'Clear water that absorbs light like Vibrant Visuals, with native animated normals and roughness'),
    ('PLAIN', 'Plain', 'Flat stylized blue with white water along every shore, like key-art renders'),
)
PRESETS = {
    # Body albedo matched to the user's gameplay recording (open water about
    # sRGB 6/68/103) under Sparkle's daylight; the game's MI_WaterOverworld
    # Colour Deep (.0006, .011, .021) rendered too purple once lit.
    'DUNGEONS': dict(tint=(.0004, .036, .032, 1), opacity=1.0),
    # Vibrant's colour comes from the owned body of water (water_body.py);
    # this is its viewport colour only.
    'VIBRANT': dict(viewport=(.021, .155, .351, 1), roughness=.10),
    # Key-art water (Omar Renders): one flat, bright blue, white at the shores.
    'PLAIN': dict(tint=(.017, .3, .83, 1), opacity=1.0),
}

# Minecraft's default water color (#3F76E4), scene-linear. Plain water takes
# biome colors relative to it, so the chosen color is the plains/ocean one.
BIOME_WATER = (.0497, .1812, .7758)
PLAIN_NIGHT = .4            # Plain water's body color at full night, as a share of its day color

# Match complete resource identifiers, including MiEx's hash and Blender's
# duplicate suffix. Substring matches would replace underwater plants/cauldrons.
_RESOURCE = re.compile(
    r'^(?:mat_)?(?:minecraft[:_](?:block[s]?[_/])?|(?:textures/)?blocks?/)?'
    r'(?:water(?:_(?:still|flow))?|flowing_water)(?:_[0-9a-f]{8,32})?$')
_EXCLUDED = re.compile(r'cauldron|underwater|waterlogged|seagrass|kelp|waterlily|bubble')


def _water_name(name, *, texture=False):
    name = str(name).replace('\\', '/').lower()
    name = re.sub(r'\.\d{3,}$', '', name)
    if texture:
        name = name.rsplit('/', 1)[-1]
        name = re.sub(r'\.(?:png|tga|jpg|jpeg|exr)$', '', name)
        # Portable MiEx textures are prefixed with a content hash.
        name = re.sub(r'^[0-9a-f]{8,32}_', '', name)
        name = re.sub(r'_(?:n|s|normal|mer)$', '', name)
    return bool(_RESOURCE.fullmatch(name))


def mark_imported(material, source_name):
    """Record the exact sidecar key after the importer has built the material."""
    material[SOURCE] = str(source_name)


def is_water_material(material):
    if not material:
        return False
    if material.get(TAG):
        return True
    name = material.get(SOURCE, material.name)
    if _EXCLUDED.search(name.lower()):
        return False
    if _water_name(name):
        return True
    # Existing imports predate provenance tags. Only accept an unambiguous
    # water-only image graph, and only use this within a stamped MiEx collection.
    tree = material.node_tree
    images = [n.image for n in tree.nodes if n.type == 'TEX_IMAGE' and n.image] if tree else []
    return bool(images) and all(_water_name(i.filepath or i.name, texture=True) for i in images)


def build_material(scene, original, preset):
    """Build an independent shader with editable, named color/value controls."""
    p = PRESETS[preset]
    material = bpy.data.materials.new('Sparkle water · ' + dict((i[0], i[1]) for i in PRESET_ITEMS)[preset])
    material[TAG] = preset
    material[VERSION_TAG] = VERSIONS[preset]
    material[SOURCE] = original.get(SOURCE, original.name)
    material['mc_edge_exclude'] = True
    material.use_nodes = True
    material.diffuse_color = p.get('viewport') or (*p['tint'][:3], p['opacity'])
    material.use_backface_culling = False
    if hasattr(material, 'surface_render_method'):
        material.surface_render_method = 'DITHERED'
    elif hasattr(material, 'blend_method'):
        material.blend_method = 'HASHED'
    if hasattr(material, 'use_raytrace_refraction'):
        # Dungeons refracts its shallows in big pixel steps; Vibrant transmits
        # unbent because its screen-space refraction hazed the whole bed.
        material.use_raytrace_refraction = preset == 'DUNGEONS'
    if hasattr(material, 'use_transparent_shadow'):
        material.use_transparent_shadow = True
    if hasattr(material, 'thickness_mode'):
        material.thickness_mode = 'SLAB'
    # Explicit application enables EEVEE ray tracing below, allowing reflections
    # and Vibrant refraction to work even without a Sparkle sky preset.
    if hasattr(material, 'refraction_depth'):
        material.refraction_depth = .25
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()

    def node(kind, name, x, y):
        result = nodes.new(kind)
        result.name = result.label = name
        result.location = (x, y)
        return result

    def value(name, amount, x, y):
        result = node('ShaderNodeValue', name, x, y)
        result.outputs[0].default_value = amount
        return result.outputs[0]

    def seconds(x, y):
        result = value('Time (seconds)', 0, x, y)
        driver = result.driver_add('default_value').driver
        driver.expression = 'frame * fps_base / fps'
        for name, path in (('fps', 'render.fps'), ('fps_base', 'render.fps_base')):
            variable = driver.variables.new()
            variable.name = name
            variable.type = 'SINGLE_PROP'
            variable.targets[0].id_type = 'SCENE'
            variable.targets[0].id = scene
            variable.targets[0].data_path = path
        return result

    if preset == 'PLAIN':
        # One flat color, nothing showing through and no reflection; the
        # shore whitewater is the owned contact sheet (water_contact.py),
        # always on for this style. Like normal water it is recolored per
        # biome (MiEx's Cd color, relative to Minecraft's default water).
        settings = getattr(scene, 'mcbedrock_world_exporter', None)
        colour = node('ShaderNodeRGB', 'Water tint', -700, 300)
        colour.outputs[0].default_value = (*settings.plain_water_color, 1) if settings else p['tint']
        biome = node('ShaderNodeVertexColor', 'Biome water', -1100, 100)
        biome.layer_name = 'Cd'
        shift = node('ShaderNodeVectorMath', 'Biome shift', -900, 100)
        shift.operation = 'DIVIDE'
        links.new(biome.outputs['Color'], shift.inputs[0])
        shift.inputs[1].default_value = BIOME_WATER
        recolored = node('ShaderNodeVectorMath', 'Biome water colour', -700, 100)
        recolored.operation = 'MULTIPLY'
        links.new(colour.outputs[0], recolored.inputs[0])
        links.new(shift.outputs[0], recolored.inputs[1])
        # Water without biome colors (no Cd) keeps the chosen color.
        brightness = node('ShaderNodeRGBToBW', 'Biome brightness', -1100, -100)
        links.new(biome.outputs['Color'], brightness.inputs[0])
        present = node('ShaderNodeMath', 'Has biome colors', -900, -100)
        present.operation = 'GREATER_THAN'
        links.new(brightness.outputs[0], present.inputs[0])
        present.inputs[1].default_value = .001
        use = node('ShaderNodeMath', 'Use biome colors', -700, -100)
        use.operation = 'MULTIPLY'
        links.new(present.outputs[0], use.inputs[0])
        links.new(value('Biome colors', 1. if settings is None or settings.plain_water_biome else 0., -900, -250),
                  use.inputs[1])
        tint = node('ShaderNodeMixRGB', 'Water colour', -450, 300)
        tint.use_clamp = True
        links.new(use.outputs[0], tint.inputs[0])
        links.new(colour.outputs[0], tint.inputs[1])
        links.new(recolored.outputs[0], tint.inputs[2])
        # Lit like the blocks: it darkens at night and in shadow, and under
        # Flat lighting (diffuse color times diffuse light) a lit face shows
        # exactly the chosen color. Trailer nights light the land, so its
        # body color also dims with the daylight the sky reports (set_daylight).
        dimmed = node('ShaderNodeVectorMath', 'Water by daylight', -300, 300)
        dimmed.operation = 'SCALE'
        links.new(tint.outputs[0], dimmed.inputs[0])
        links.new(value('Daylight', 1., -450, 450), dimmed.inputs['Scale'])
        flat = node('ShaderNodeBsdfDiffuse', 'Water surface', -150, 300)
        links.new(dimmed.outputs[0], flat.inputs['Color'])
        output = node('ShaderNodeOutputMaterial', 'Water output', 100, 300)
        links.new(flat.outputs[0], output.inputs['Surface'])
        return material

    if preset == 'DUNGEONS':
        # Dungeons reads water depth from a per-block map baked at apply time
        # (water_depth.py): Blender surface shaders cannot see what lies beneath.
        depth_parts = node('ShaderNodeSeparateXYZ', 'Depth map position', -1240, 300)
        links.new(node('ShaderNodeNewGeometry', 'Depth map geometry', -1450, 300).outputs['Position'],
                  depth_parts.inputs[0])
        depth_uv = node('ShaderNodeCombineXYZ', 'Depth map coordinates', -820, 300)
        for axis, offset in (('X', 0), ('Y', 0)):
            shifted = node('ShaderNodeMath', 'Depth map ' + axis + ' offset', -1020, 300)
            shifted.operation = 'SUBTRACT'
            links.new(depth_parts.outputs[axis], shifted.inputs[0])
            links.new(value('Depth map origin ' + axis, offset, -1240, 100), shifted.inputs[1])
            scaled = node('ShaderNodeMath', 'Depth map ' + axis + ' scale', -900, 300)
            scaled.operation = 'DIVIDE'
            links.new(shifted.outputs[0], scaled.inputs[0])
            links.new(value('Depth map size ' + axis, 1, -1240, 0), scaled.inputs[1])
            links.new(scaled.outputs[0], depth_uv.inputs[axis])
        depth_map = node('ShaderNodeTexImage', 'Water depth map', -650, 300)
        depth_map.interpolation, depth_map.extension = 'Linear', 'EXTEND'
        links.new(depth_uv.outputs[0], depth_map.inputs['Vector'])
        blocks = node('ShaderNodeMath', 'Water depth (blocks)', -420, 450)
        blocks.operation = 'MULTIPLY'
        blocks.inputs[1].default_value = water_depth.MAX_DEPTH
        links.new(depth_map.outputs['Color'], blocks.inputs[0])

    if preset == 'DUNGEONS':
        # Geometry stays flat; authored fields perturb its shading normal on
        # a fixed pixel grid. Contact geometry supplies the separate outline.
        # Gameplay shows one even blue body; only the white contact outline
        # marks the shore. A shallow-to-deep blend over baked depth produced
        # pale cyan shallows that the game does not show. The body uses the
        # game's MI_WaterOverworld Colour Deep, lit and reflected.
        tint = node('ShaderNodeRGB', 'Water tint', -400, 300)
        tint.outputs[0].default_value = p['tint']
        # Both textures are referenced by the cooked water master, but its
        # editor graph is absent. These projections are a Blender adaptation.
        # The 16px flow texture has only two bright green pixels: using that
        # channel on lakes produced conspicuous, regularly repeated 1/4-block
        # dashes. The native 64px pixel mask gives irregular 1/16-block flecks.
        geometry = node('ShaderNodeNewGeometry', 'Water face and position', -1450, 650)
        position = node('ShaderNodeSeparateXYZ', 'Effects position', -1240, 700)
        normal = node('ShaderNodeSeparateXYZ', 'Water face normal', -1240, 1050)
        links.new(geometry.outputs['Position'], position.inputs[0])
        links.new(geometry.outputs['True Normal'], normal.inputs[0])
        up = node('ShaderNodeMath', 'Horizontal water', -1020, 1100)
        up.operation = 'ABSOLUTE'
        links.new(normal.outputs['Z'], up.inputs[0])
        vertical = node('ShaderNodeMath', 'Vertical water', -820, 1100)
        vertical.operation = 'LESS_THAN'
        vertical.inputs[1].default_value = .5
        links.new(up.outputs[0], vertical.inputs[0])

        def vector(operation, name, first, second=None, scale=None):
            result = node('ShaderNodeVectorMath', name, -1700, -400 - len(nodes) * 35)
            result.operation = operation
            for index, source in enumerate((first, second)):
                if source is None:
                    continue
                if isinstance(source, tuple):
                    result.inputs[index].default_value = source
                else:
                    links.new(source, result.inputs[index])
            if scale is not None:
                if isinstance(scale, (int, float)):
                    result.inputs['Scale'].default_value = scale
                else:
                    links.new(scale, result.inputs['Scale'])
            return result.outputs[0]

        # The cooked master references both textures, world-space normals,
        # FlattenNormal and 2D-offset refraction. Its editor links are absent:
        # this construction is an adaptation, not a recovered normal map.
        # Snap BEFORE motion/sampling so every channel stays constant within
        # the same world-space cell, even while the fields animate. The
        # gameplay recordings step distortion and tone in quarter-block
        # squares (diamonds from the game's 45-degree camera), not 1/16.
        grid = vector('SCALE', 'Wave pixel index', geometry.outputs['Position'], scale=DUNGEONS_CELLS)
        grid = vector('FLOOR', 'Wave pixel floor', grid)
        grid = vector('ADD', 'Wave pixel center', grid, (.5, .5, .5))
        grid = vector('SCALE', 'Wave pixel position', grid, scale=1 / DUNGEONS_CELLS)
        clock = seconds(-2100, -400)
        wave_time = node('ShaderNodeMath', 'Wave motion', -1900, -400)
        wave_time.operation = 'MULTIPLY'
        links.new(clock, wave_time.inputs[0])
        # Blocks per second; the creek recording drifts visibly.
        links.new(value('Wave speed', .4, -2100, -600), wave_time.inputs[1])
        drift = node('ShaderNodeCombineXYZ', 'Wave drift', -1700, -400)
        links.new(wave_time.outputs[0], drift.inputs['X'])
        cross_drift = node('ShaderNodeMath', 'Wave cross drift', -1900, -600)
        cross_drift.operation = 'MULTIPLY'
        cross_drift.inputs[1].default_value = .63
        links.new(wave_time.outputs[0], cross_drift.inputs[0])
        links.new(cross_drift.outputs[0], drift.inputs['Y'])
        sample_position = vector('ADD', 'Animated wave position', grid, drift.outputs[0])

        def field(filename, name, coordinates, interpolation):
            image = bpy.data.images.load(str(Path(__file__).resolve().parents[1] /
                'assets/dungeons_ii' / filename), check_existing=True)
            image.colorspace_settings.name = 'Non-Color'
            if not image.packed_file:
                image.pack()
            result = node('ShaderNodeTexImage', name, -1400, -600 - len(nodes) * 35)
            result.image, result.interpolation, result.extension = image, interpolation, 'REPEAT'
            links.new(coordinates, result.inputs['Vector'])
            return result.outputs['Color']

        cloud_uv = vector('SCALE', 'Broad wave coordinates', sample_position, scale=.25)
        components = []
        for axis, offset in (('X', (0, 0, 0)), ('Y', (.43, .27, 0))):
            coordinates = vector('ADD', 'Broad wave offset ' + axis, cloud_uv, offset)
            sample = field('T_SeamlessClouds.png', 'Native broad field ' + axis, coordinates, 'Linear')
            signed = node('ShaderNodeMath', 'Signed broad field ' + axis, -1000, -850)
            signed.operation = 'SUBTRACT'
            links.new(sample, signed.inputs[0])
            signed.inputs[1].default_value = .5
            components.append(signed.outputs[0])
        broad = node('ShaderNodeCombineXYZ', 'Broad wave field', -800, -850)
        links.new(components[0], broad.inputs['X'])
        links.new(components[1], broad.inputs['Y'])
        # One texel of the 32px field per cell.
        detail_uv = vector('SCALE', 'Fine wave coordinates', sample_position, scale=DUNGEONS_CELLS / 32)
        # Interpolate the moving field, not the world-space cell boundaries.
        # Closest held each binary value before abruptly jumping a whole cell;
        # Linear lets a fixed square's normal evolve continuously over time.
        detail = field('T_PixelNoise_Bright.png', 'Native fine field', detail_uv, 'Linear')
        detail = vector('SUBTRACT', 'Signed fine field', detail, (.5, .5, .5))
        # The gameplay recording shows small moving squares of lighter and
        # darker blue across the whole surface. Drive them from the same fine
        # field as body brightness rather than as normals, so they never
        # scatter reflections.
        tone_parts = node('ShaderNodeSeparateXYZ', 'Fine field tone', -800, -1250)
        links.new(detail, tone_parts.inputs[0])
        pixel_tone = node('ShaderNodeMath', 'Surface pixel tone', -600, -1250)
        pixel_tone.operation = 'MULTIPLY_ADD'
        links.new(tone_parts.outputs['X'], pixel_tone.inputs[0])
        # Measured in the recording: about 4% brightness spread.
        links.new(value('Surface pixel variation', .2, -800, -1400), pixel_tone.inputs[1])
        pixel_tone.inputs[2].default_value = 1
        # The pier and harbor references shift reflections in coherent square
        # patches with stepped edges. The binary fine field changes direction
        # independently in every 1/16 cell; dominating the normal, it scattered
        # reflections into orange/blue per-cell speckle at sunset. The smooth
        # broad field now leads and the fine field only roughens patch edges.
        broad_field = vector('SCALE', 'Broad wave amount', broad.outputs[0],
                             scale=value('Broad wave mix', 1.0, -1000, -1050))
        detail = vector('SCALE', 'Fine wave amount', detail,
                        scale=value('Fine wave mix', .15, -1000, -1150))
        waves = vector('ADD', 'Broad and fine wave field', broad_field, detail)
        waves = vector('SCALE', 'Wave amount', waves, scale=value('Wave strength', .3, -800, -1050))
        # Small normal changes become huge reflected-sky jumps at grazing
        # angles, especially once a world pixel is subpixel in the image.
        # Retain the selected close/oblique look, flattening only those views.
        # The source's CameraDistanceFade supports a distance response, but
        # these block distances and view thresholds are our Blender adaptation.
        angle = node('ShaderNodeVectorMath', 'Water view cosine', -1900, -1300)
        angle.operation = 'DOT_PRODUCT'
        links.new(geometry.outputs['True Normal'], angle.inputs[0])
        links.new(geometry.outputs['Incoming'], angle.inputs[1])
        cosine = node('ShaderNodeMath', 'Absolute water view cosine', -1700, -1300)
        cosine.operation = 'ABSOLUTE'
        links.new(angle.outputs['Value'], cosine.inputs[0])

        def fade(name, source, near, far, first, last):
            result = node('ShaderNodeMapRange', name, -1450, -1300 - len(nodes) * 35)
            result.interpolation_type = 'SMOOTHSTEP'
            result.clamp = True
            links.new(source, result.inputs['Value'])
            for key, amount in (('From Min', near), ('From Max', far),
                                ('To Min', first), ('To Max', last)):
                result.inputs[key].default_value = amount
            return result.outputs[0]

        angle_fade = fade('Grazing wave fade', cosine.outputs[0], .12, .45, 0, 1)
        camera = node('ShaderNodeCameraData', 'Water camera distance', -1700, -1600)
        distance_fade = fade('Distant wave fade', camera.outputs['View Distance'], 32, 96, 1, 0)
        visibility = node('ShaderNodeMath', 'Water detail visibility', -1200, -1300)
        visibility.operation = 'MULTIPLY'
        links.new(angle_fade, visibility.inputs[0])
        links.new(distance_fade, visibility.inputs[1])
        waves = vector('SCALE', 'Visible wave amount', waves, scale=visibility.outputs[0])
        waved = vector('NORMALIZE', 'Pixel wave normal', vector(
            'ADD', 'Perturbed wave normal', geometry.outputs['True Normal'], waves))
        face_normal = node('ShaderNodeMixRGB', 'Surface wave normal', -500, -650)
        links.new(vertical.outputs[0], face_normal.inputs[0])
        links.new(waved, face_normal.inputs[1])
        links.new(geometry.outputs['True Normal'], face_normal.inputs[2])
        motion = node('ShaderNodeMath', 'Effects motion', -1240, 50)
        motion.operation = 'MULTIPLY'
        links.new(clock, motion.inputs[0])
        links.new(value('Effects speed', .05, -1450, -120), motion.inputs[1])
        offset = node('ShaderNodeCombineXYZ', 'Flow offset', -1020, 50)
        links.new(motion.outputs[0], offset.inputs['Y'])
        along = node('ShaderNodeMath', 'Along vertical face', -1020, 470)
        along.operation = 'ADD'
        links.new(position.outputs['X'], along.inputs[0])
        links.new(position.outputs['Y'], along.inputs[1])
        projection = node('ShaderNodeCombineXYZ', 'Vertical flow projection', -820, 470)
        links.new(along.outputs[0], projection.inputs['X'])
        links.new(position.outputs['Z'], projection.inputs['Y'])
        channels = []
        for label, coordinates, row, filename in (
                ('Surface', geometry.outputs['Position'], 820, 'T_PixelNoise.png'),
                ('Vertical', projection.outputs[0], 470, 'T_SpicewoodWaterFlow.png')):
            image = bpy.data.images.load(str(Path(__file__).resolve().parents[1] /
                'assets/dungeons_ii' / filename), check_existing=True)
            image.colorspace_settings.name = 'Non-Color'
            if not image.packed_file:
                image.pack()
            scale_node = node('ShaderNodeVectorMath', label + ' effects scale', -610, row)
            scale_node.operation = 'SCALE'
            # Surface flecks: one 64px-mask texel per pattern cell, as the
            # recording's white squares are. Waterfall streaks keep .25.
            scale_node.inputs['Scale'].default_value = DUNGEONS_CELLS / 64 if label == 'Surface' else .25
            links.new(coordinates, scale_node.inputs[0])
            moving = node('ShaderNodeVectorMath', label + ' flow coordinates', -390, row)
            moving.operation = 'ADD'
            links.new(scale_node.outputs[0], moving.inputs[0])
            links.new(offset.outputs[0], moving.inputs[1])
            texture = node('ShaderNodeTexImage', label + ' source flow mask', -160, row)
            texture.image = image
            texture.interpolation = 'Closest'
            texture.extension = 'REPEAT'
            links.new(moving.outputs[0], texture.inputs['Vector'])
            channel = node('ShaderNodeSeparateColor', label + ' flow channels', 80, row)
            links.new(texture.outputs['Color'], channel.inputs[0])
            channels.append(channel)
        # The recording's flecks are bright and sparse, a few together, about
        # one cluster per five square blocks. Gate the mask to the brightest
        # 6% of the drifting broad field so they gather and move.
        fleck_gate = node('ShaderNodeMath', 'Fleck cluster gate', 80, 900)
        fleck_gate.operation = 'GREATER_THAN'
        links.new(components[0], fleck_gate.inputs[0])
        fleck_gate.inputs[1].default_value = .17
        gated = node('ShaderNodeMath', 'Clustered fleck mask', 200, 900)
        gated.operation = 'MULTIPLY'
        links.new(channels[0].outputs['Red'], gated.inputs[0])
        links.new(fleck_gate.outputs[0], gated.inputs[1])
        flecks = node('ShaderNodeMath', 'Sparse surface flecks', 300, 850)
        flecks.operation = 'MULTIPLY'
        links.new(gated.outputs[0], flecks.inputs[0])
        links.new(value('Surface fleck strength', 4, 80, 1050), flecks.inputs[1])
        streaks = node('ShaderNodeMath', 'Vertical streaks', 300, 470)
        streaks.operation = 'MULTIPLY_ADD'
        streaks.inputs[1].default_value = .3
        links.new(channels[1].outputs['Blue'], streaks.inputs[0])
        links.new(channels[1].outputs['Red'], streaks.inputs[2])
        pattern = node('ShaderNodeMixRGB', 'Face-specific flow effects', 520, 720)
        links.new(vertical.outputs[0], pattern.inputs[0])
        links.new(flecks.outputs[0], pattern.inputs[1])
        links.new(streaks.outputs[0], pattern.inputs[2])
        amount = node('ShaderNodeMath', 'Flow effects amount', 740, 720)
        amount.operation = 'MULTIPLY'
        amount.use_clamp = True
        links.new(pattern.outputs[0], amount.inputs[0])
        links.new(value('Effects strength', .18, 520, 1000), amount.inputs[1])
        effects = node('ShaderNodeMixRGB', 'Water with flow effects', 950, 250)
        effect_color = node('ShaderNodeMixRGB', 'Surface and waterfall tint', 740, 450)
        effect_color.inputs[1].default_value = (.8, .9, 1, 1)
        effect_color.inputs[2].default_value = (.17708331, .60225517, 1, 1)
        links.new(vertical.outputs[0], effect_color.inputs[0])
        links.new(effect_color.outputs[0], effects.inputs[2])
        links.new(amount.outputs[0], effects.inputs[0])
        # Fade the tone with the waves: distant 1/16 cells are subpixel and
        # would otherwise read as grain.
        tone_amount = node('ShaderNodeMath', 'Visible pixel variation', -700, -1400)
        tone_amount.operation = 'MULTIPLY'
        links.new(nodes['Surface pixel variation'].outputs[0], tone_amount.inputs[0])
        tone_fade = node('ShaderNodeMapRange', 'Distant pixel tone fade', -900, -1500)
        tone_fade.interpolation_type = 'SMOOTHSTEP'
        links.new(nodes['Water camera distance'].outputs['View Distance'], tone_fade.inputs['Value'])
        for key, amount in (('From Min', 32), ('From Max', 96), ('To Min', 1), ('To Max', 0)):
            tone_fade.inputs[key].default_value = amount
        tone_visibility = node('ShaderNodeMath', 'Pixel tone visibility', -800, -1500)
        tone_visibility.operation = 'MULTIPLY'
        links.new(visibility.outputs[0], tone_visibility.inputs[0])
        links.new(tone_fade.outputs[0], tone_visibility.inputs[1])
        links.new(tone_visibility.outputs[0], tone_amount.inputs[1])
        links.new(tone_amount.outputs[0], pixel_tone.inputs[1])
        toned = node('ShaderNodeMixRGB', 'Water with pixel tone', 740, 200)
        toned.blend_type = 'MULTIPLY'
        toned.inputs[0].default_value = 1
        # Deeper water reads bluer: the body blends from the game's teal toward
        # a deep blue over Colour depth, from the same per-block depth that
        # limits the see-through.
        colour_depth = node('ShaderNodeMath', 'Relative colour depth', -400, 600)
        colour_depth.operation = 'DIVIDE'
        links.new(blocks.outputs[0], colour_depth.inputs[0])
        links.new(value('Colour depth (blocks)', 2.5, -600, 650), colour_depth.inputs[1])
        fade = node('ShaderNodeMath', 'Colour depth fade', -250, 600)
        fade.operation = 'EXPONENT'
        negative_depth = node('ShaderNodeMath', 'Negative colour depth', -320, 600)
        negative_depth.operation = 'MULTIPLY'
        negative_depth.inputs[1].default_value = -1
        links.new(colour_depth.outputs[0], negative_depth.inputs[0])
        links.new(negative_depth.outputs[0], fade.inputs[0])
        deepness = node('ShaderNodeMath', 'Water deepness', -100, 600)
        deepness.operation = 'SUBTRACT'
        deepness.inputs[0].default_value = 1
        links.new(fade.outputs[0], deepness.inputs[1])
        deep_body = node('ShaderNodeRGB', 'Deep water colour', -400, 800)
        deep_body.outputs[0].default_value = (.0015, .022, .11, 1)
        body = node('ShaderNodeMixRGB', 'Water colour by depth', 100, 500)
        links.new(deepness.outputs[0], body.inputs[0])
        links.new(tint.outputs[0], body.inputs[1])
        links.new(deep_body.outputs[0], body.inputs[2])
        links.new(body.outputs[0], toned.inputs[1])
        links.new(pixel_tone.outputs[0], toned.inputs[2])
        links.new(toned.outputs[0], effects.inputs[1])
        opacity = value('Opacity', p['opacity'], -400, -50)
        # The creek recording shows the bed through shallow water, teal
        # tinted and shifted in quarter-block steps as the water flows; deep
        # water is the opaque blue body. The game uses a 2D-offset refraction;
        # here the pixel-stepped wave normal bends a refractive transmission,
        # and the baked depth map limits it to the shallows.
        # EEVEE drops a Refraction BSDF mixed with a separate Glossy BSDF (the
        # bed vanished under the sky) and a single Principled transmission
        # veils the bed in its pale base colour. A dark diffuse body mixed with
        # tinted refraction, plus a black Principled specular layer for the
        # reflections, renders the bed in both engines. Version 4 refracted 60%
        # everywhere, so deep-water rays missed and EEVEE fell back to the
        # orange sunset probe.
        clear_depth = node('ShaderNodeMath', 'Shallow clarity', 950, -950)
        clear_depth.operation = 'EXPONENT'
        relative = node('ShaderNodeMath', 'Relative clear depth', 800, -950)
        relative.operation = 'DIVIDE'
        links.new(blocks.outputs[0], relative.inputs[0])
        # You see about two blocks down; past three the water is the body colour.
        links.new(value('Clear depth (blocks)', 1.1, 650, -1000), relative.inputs[1])
        negative = node('ShaderNodeMath', 'Negative clear depth', 880, -950)
        negative.operation = 'MULTIPLY'
        negative.inputs[1].default_value = -1
        links.new(relative.outputs[0], negative.inputs[0])
        links.new(negative.outputs[0], clear_depth.inputs[0])
        through = node('ShaderNodeMath', 'See-through amount', 1180, -950)
        through.operation = 'MULTIPLY_ADD'
        through.use_clamp = True
        links.new(clear_depth.outputs[0], through.inputs[0])
        # Bright sand seen through barely tinted water glowed along the shore.
        links.new(value('Shallow see-through', .55, 950, -1100), through.inputs[1])
        links.new(value('Transmission', 0, 1180, -800), through.inputs[2])
        transmission_amount = through.outputs[0]
        shader = node('ShaderNodeBsdfDiffuse', 'Water surface', 1180, 250)
        links.new(effects.outputs[0], shader.inputs['Color'])
        links.new(face_normal.outputs[0], shader.inputs['Normal'])
        refraction = node('ShaderNodeBsdfRefraction', 'Water see-through', 1180, -550)
        refraction.inputs['Roughness'].default_value = 0
        links.new(value('Refraction IOR', 1.333, 950, -600), refraction.inputs['IOR'])
        links.new(face_normal.outputs[0], refraction.inputs['Normal'])
        # A colour overlay on whatever lies under the water: cyan in the
        # shallows, turning blue with depth, like the game's water.
        shallow_tint = node('ShaderNodeRGB', 'Transmission tint', 750, -750)
        shallow_tint.outputs[0].default_value = (.14, .55, .62, 1)
        deep_tint = node('ShaderNodeRGB', 'Deep transmission tint', 750, -900)
        deep_tint.outputs[0].default_value = (.03, .2, .55, 1)
        shallow = node('ShaderNodeMixRGB', 'Underwater overlay', 950, -750)
        links.new(nodes['Water deepness'].outputs[0], shallow.inputs[0])
        links.new(shallow_tint.outputs[0], shallow.inputs[1])
        links.new(deep_tint.outputs[0], shallow.inputs[2])
        links.new(shallow.outputs[0], refraction.inputs['Color'])
        transmission = node('ShaderNodeMixShader', 'Water transmission', 1470, -350)
        links.new(transmission_amount, transmission.inputs[0])
        links.new(shader.outputs[0], transmission.inputs[1])
        links.new(refraction.outputs[0], transmission.inputs[2])
        reflection = node('ShaderNodeBsdfPrincipled', 'Water reflections', 1180, -150)
        reflection.inputs['Base Color'].default_value = (0, 0, 0, 1)
        reflection.inputs['Roughness'].default_value = 0
        links.new(face_normal.outputs[0], reflection.inputs['Normal'])
        links.new(nodes['Refraction IOR'].outputs[0], reflection.inputs['IOR'])
        # Reflection strength is the reflectance seen looking straight down;
        # Fresnel raises it toward the horizon. At IOR 1.333, F0 is about
        # .0408 x the level.
        level = node('ShaderNodeMath', 'Reflection level', 950, -1300)
        level.operation = 'DIVIDE'
        links.new(value('Reflection strength', DUNGEONS_REFLECTION, 750, -1300), level.inputs[0])
        level.inputs[1].default_value = .0408
        links.new(level.outputs[0], reflection.inputs['Specular IOR Level'])
        # Most visible brightness is reflected sky, so the pixel tone tints the
        # reflection as well as the body.
        links.new(pixel_tone.outputs[0], reflection.inputs['Specular Tint'])
        blend = node('ShaderNodeAddShader', 'Restrained reflections', 1470, 250)
        links.new(transmission.outputs[0], blend.inputs[0])
        links.new(reflection.outputs[0], blend.inputs[1])
        transparent = node('ShaderNodeBsdfTransparent', 'Water transparency', 1470, -100)
        visible = node('ShaderNodeMixShader', 'Water opacity', 1700, 250)
        links.new(opacity, visible.inputs[0])
        links.new(transparent.outputs[0], visible.inputs[1])
        links.new(blend.outputs[0], visible.inputs[2])
        output = node('ShaderNodeOutputMaterial', 'Water output', 1930, 250)
        links.new(visible.outputs[0], output.inputs['Surface'])
        links.new(value('Optical thickness', .25, 1700, -250), output.inputs['Thickness'])
        # Cycles shadow rays cannot follow refractive caustic paths. Let the
        # editable transmission admit direct bed lighting without altering the
        # camera/reflection closure or relying on transparent surface opacity.
        output.target = 'EEVEE'
        rays = node('ShaderNodeLightPath', 'Water light paths', 1700, -600)
        shadow = node('ShaderNodeBsdfTransparent', 'Underwater direct light', 1700, -850)
        shadow_tint = node('ShaderNodeMixRGB', 'Underwater light tint', 1470, -850)
        shadow_tint.blend_type = 'MULTIPLY'
        shadow_tint.inputs[0].default_value = 1
        links.new(shallow.outputs[0], shadow_tint.inputs[1])
        links.new(transmission_amount, shadow_tint.inputs[2])
        shadow_opacity = node('ShaderNodeMixRGB', 'Underwater light opacity', 1700, -1100)
        shadow_opacity.inputs[1].default_value = (1, 1, 1, 1)
        links.new(opacity, shadow_opacity.inputs[0])
        links.new(shadow_tint.outputs[0], shadow_opacity.inputs[2])
        links.new(shadow_opacity.outputs[0], shadow.inputs['Color'])
        shadow_mix = node('ShaderNodeMixShader', 'Cycles underwater illumination', 1930, -450)
        links.new(rays.outputs['Is Shadow Ray'], shadow_mix.inputs[0])
        links.new(visible.outputs[0], shadow_mix.inputs[1])
        links.new(shadow.outputs[0], shadow_mix.inputs[2])
        cycles_output = node('ShaderNodeOutputMaterial', 'Water output (Cycles)', 2150, -450)
        cycles_output.target = 'CYCLES'
        links.new(shadow_mix.outputs[0], cycles_output.inputs['Surface'])
        # EEVEE shadows treat a refraction closure as opaque, which left the
        # visible shallows unlit and far darker than Cycles. Its shadow pass
        # also sets Is Shadow Ray, so it shares the transmitted-light branch.
        # Its screen-space refraction rarely found the bed either, so EEVEE
        # sees through with the overlay as a tinted Transparent BSDF instead.
        overlay = node('ShaderNodeBsdfTransparent', 'Underwater overlay (EEVEE)', 1180, -700)
        links.new(shallow.outputs[0], overlay.inputs['Color'])
        see_through = node('ShaderNodeMixShader', 'Water transmission (EEVEE)', 1470, -550)
        links.new(transmission_amount, see_through.inputs[0])
        links.new(shader.outputs[0], see_through.inputs[1])
        links.new(overlay.outputs[0], see_through.inputs[2])
        eevee_blend = node('ShaderNodeAddShader', 'Restrained reflections (EEVEE)', 1700, 0)
        links.new(see_through.outputs[0], eevee_blend.inputs[0])
        links.new(reflection.outputs[0], eevee_blend.inputs[1])
        eevee_visible = node('ShaderNodeMixShader', 'Water opacity (EEVEE)', 1930, 0)
        links.new(opacity, eevee_visible.inputs[0])
        links.new(transparent.outputs[0], eevee_visible.inputs[1])
        links.new(eevee_blend.outputs[0], eevee_visible.inputs[2])
        eevee_shadow = node('ShaderNodeMixShader', 'EEVEE underwater illumination', 2150, 0)
        links.new(rays.outputs['Is Shadow Ray'], eevee_shadow.inputs[0])
        links.new(eevee_visible.outputs[0], eevee_shadow.inputs[1])
        links.new(shadow.outputs[0], eevee_shadow.inputs[2])
        links.new(eevee_shadow.outputs[0], output.inputs['Surface'])
        output.is_active_output = cycles_output.is_active_output = True
        return material

    # Vanilla Vibrant disables procedural waves. Its authored normal/MERS
    # flipbooks provide the pixel surface; sample every channel on one 1/16
    # block grid rather than mixing procedural frequencies.
    def math_node(operation, name, a, b=None, c=None):
        result = node('ShaderNodeMath', name, -900, 200)
        result.operation = operation
        for index, source in enumerate((a, b, c)):
            if source is None:
                continue
            if isinstance(source, (int, float)):
                result.inputs[index].default_value = source
            else:
                links.new(source, result.inputs[index])
        return result.outputs[0]

    def vector_node(operation, name, a, b=None):
        result = node('ShaderNodeVectorMath', name, -600, 600)
        result.operation = operation
        for index, source in enumerate((a, b)):
            if source is None:
                continue
            if isinstance(source, tuple):
                result.inputs[index].default_value = source
            else:
                links.new(source, result.inputs[index])
        return result.outputs['Value' if operation == 'DOT_PRODUCT' else 'Vector']

    time = seconds(-1500, 900)
    speed = value('Animation speed', 1.0, -1500, 700)
    time = math_node('MULTIPLY', 'Animation time', time, speed)
    strength = value('Wave strength', 1.0, -1500, 500)
    # Vibrant's biome surface colour acts as a dye in otherwise clear water.
    # White adds none, as in the vanilla settings. EEVEE's dithered surface
    # transmits only the tint's brightness.
    tint = node('ShaderNodeRGB', 'Water tint', -1500, 100)
    tint.outputs[0].default_value = (1, 1, 1, 1)
    geometry = node('ShaderNodeNewGeometry', 'World position', -1500, 1300)
    normal_parts = node('ShaderNodeSeparateXYZ', 'Face direction', -1300, 1300)
    links.new(geometry.outputs['True Normal'], normal_parts.inputs[0])
    vertical = math_node('LESS_THAN', 'Vertical water',
                        math_node('ABSOLUTE', 'Face uprightness', normal_parts.outputs['Z']), .5)
    tangent_side = vector_node('NORMALIZE', 'Side tangent', vector_node(
        'CROSS_PRODUCT', 'Side direction', (0, 0, 1), geometry.outputs['True Normal']))
    tangent_top = vector_node('NORMALIZE', 'Top tangent', vector_node(
        'CROSS_PRODUCT', 'Top direction', (0, 1, 0), geometry.outputs['True Normal']))
    tangent = node('ShaderNodeMixRGB', 'Water tangent', -1050, 1200)
    links.new(tangent_top, tangent.inputs[1])
    links.new(vertical, tangent.inputs[0])
    links.new(tangent_side, tangent.inputs[2])
    bitangent = vector_node('CROSS_PRODUCT', 'Water bitangent',
                            geometry.outputs['True Normal'], tangent.outputs[0])
    u = vector_node('DOT_PRODUCT', 'Surface U', geometry.outputs['Position'], tangent.outputs[0])
    v = vector_node('DOT_PRODUCT', 'Surface V', geometry.outputs['Position'], bitangent)
    # Sampling at cell centers also aligns the lower-resolution flow MERS map
    # to the same grid. Larger authored patches occupy whole cells, never
    # introduce another fractional-pixel pattern.
    def pixel_coordinate(source, axis):
        cells = math_node('MULTIPLY', axis + ' pixel index', source, 16)
        cells = math_node('FLOOR', axis + ' pixel floor', cells)
        return math_node('MULTIPLY_ADD', axis + ' pixel center', cells, 1 / 16, 1 / 32)
    u, v = pixel_coordinate(u, 'U'), pixel_coordinate(v, 'V')
    flow_source = bool(re.search(r'water_flow|flowing_water', material[SOURCE].lower()))
    maps = []
    for label, tile, fps in (('still', 1, 10), ('flow', 2, 20)):
        # Still timing is explicitly 2 Minecraft ticks/frame. Flow's omitted
        # tick default is approximated by 1 tick/frame, exposed via speed.
        frame = math_node('MULTIPLY', label + ' frame time', time, fps)
        frame = math_node('FLOOR', label + ' frame number', frame)
        frame = math_node('MODULO', label + ' frame loop', frame, 32)
        top = math_node('SUBTRACT', label + ' top frame', 31, frame)
        local_u = math_node('FRACT', label + ' tile U', math_node('DIVIDE', label + ' U scale', u, tile))
        local_v = math_node('FRACT', label + ' tile V', math_node('DIVIDE', label + ' V scale', v, tile))
        atlas_v = math_node('DIVIDE', label + ' atlas V', math_node('ADD', label + ' frame V', top, local_v), 32)
        uv = node('ShaderNodeCombineXYZ', label + ' atlas UV', -650, 1000)
        links.new(local_u, uv.inputs['X'])
        links.new(atlas_v, uv.inputs['Y'])
        channels = []
        for kind in ('normal', 'mers'):
            image = bpy.data.images.load(str(Path(__file__).resolve().parents[1] /
                'assets' / 'vibrant_visuals' / f'water_{label}_grey_{kind}.png'), check_existing=True)
            image.colorspace_settings.name = 'Non-Color'
            if not image.packed_file:
                image.pack()
            texture = node('ShaderNodeTexImage', label.title() + ' native ' + kind, -420, 1000)
            texture.image = image
            texture.interpolation = 'Closest'
            texture.extension = 'REPEAT'
            links.new(uv.outputs[0], texture.inputs['Vector'])
            channels.append(texture.outputs['Color'])
        maps.append(channels)
    selected = []
    for index, label in enumerate(('Native normal', 'Native MERS')):
        mix = node('ShaderNodeMixRGB', label, -200, 950 - index * 200)
        if flow_source:
            mix.inputs[0].default_value = 1
        else:
            links.new(vertical, mix.inputs[0])
        links.new(maps[0][index], mix.inputs[1])
        links.new(maps[1][index], mix.inputs[2])
        selected.append(mix.outputs[0])
    decoded = vector_node('SUBTRACT', 'Decoded native normal', vector_node(
        'SCALE', 'Normal doubled', selected[0]), (1, 1, 1))
    # SCALE's scalar socket is distinct from the second vector input.
    nodes['Normal doubled'].inputs['Scale'].default_value = 2
    components = node('ShaderNodeSeparateXYZ', 'Tangent normal components', 0, 1100)
    links.new(decoded, components.inputs[0])
    terms = []
    for axis, basis in zip('XYZ', (tangent.outputs[0], bitangent, geometry.outputs['True Normal'])):
        amount = math_node('MULTIPLY', axis + ' normal strength', components.outputs[axis], strength)
        if axis == 'Z':
            amount = math_node('ADD', 'Normal flat blend', amount,
                               math_node('SUBTRACT', 'Normal flat weight', 1, strength))
        term = node('ShaderNodeVectorMath', axis + ' normal basis', 200, 1100)
        term.operation = 'SCALE'
        links.new(basis, term.inputs[0])
        links.new(amount, term.inputs['Scale'])
        terms.append(term.outputs[0])
    native_normal = vector_node('NORMALIZE', 'Water normals', vector_node('ADD', 'Native tangent normal',
        vector_node('ADD', 'Native XY normal', terms[0], terms[1]), terms[2]))
    mers = node('ShaderNodeSeparateColor', 'Native surface properties', 200, 400)
    links.new(selected[1], mers.inputs[0])
    # The native DeferredWater GGX path clamps roughness to .05 and uses
    # dielectric F0=.02. MERS red is not a metallic water response.
    roughness = math_node('MAXIMUM', 'Native roughness floor', mers.outputs['Blue'], .05)
    # Straight-through transmission plus Fresnel-weighted GGX reflection.
    # Version 2 used Principled refraction: EEVEE's screen-space refraction
    # veiled the bed in a pale haze, and rays that missed the screen fell back
    # to the world probe, which is orange at sunset, so shorelines gained
    # orange flecks. Cycles bent the same rays into strong staircase streaks.
    # One unbent closure renders alike in both engines and lets Cycles shadow
    # rays reach the bed without a light-path switch. Absorption and
    # scattering by depth happen in the owned body of water below, from
    # Vibrant's particle concentrations.
    see_through = node('ShaderNodeBsdfTransparent', 'Water see-through', 650, 750)
    links.new(tint.outputs[0], see_through.inputs['Color'])
    reflection = node('ShaderNodeBsdfAnisotropic', 'Water reflections', 650, 400)
    reflection.inputs['Color'].default_value = (1, 1, 1, 1)
    links.new(roughness, reflection.inputs['Roughness'])
    links.new(native_normal, reflection.inputs['Normal'])
    fresnel = node('ShaderNodeFresnel', 'Water fresnel', 650, 150)
    fresnel.inputs['IOR'].default_value = 1.333
    links.new(native_normal, fresnel.inputs['Normal'])
    shader = node('ShaderNodeMixShader', 'Water surface', 850, 500)
    links.new(fresnel.outputs[0], shader.inputs[0])
    links.new(see_through.outputs[0], shader.inputs[1])
    links.new(reflection.outputs[0], shader.inputs[2])
    output = node('ShaderNodeOutputMaterial', 'Water output', 1100, 500)
    links.new(shader.outputs[0], output.inputs['Surface'])
    output.is_active_output = True
    # Arrange the generated dependency graph so its native texture inputs and
    # editable controls remain inspectable in the Shader Editor.
    depths, rows = {}, {}
    def depth(item):
        if item not in depths:
            parents = [link.from_node for socket in item.inputs for link in socket.links]
            depths[item] = 1 + max((depth(parent) for parent in parents), default=-1)
        return depths[item]
    for item in nodes:
        column = depth(item)
        row = rows.get(column, 0)
        item.location = (column * 250, -row * 240)
        rows[column] = row + 1
    return material


def set_daylight(scene, day):
    """Dim Plain water's body color toward night (day 1 by day, 0 at night)."""
    amount = PLAIN_NIGHT + (1 - PLAIN_NIGHT) * max(0., min(1., day))
    for material in bpy.data.materials:
        if material.get(TAG) != 'PLAIN' or material.node_tree is None or material.library:
            continue
        node = material.node_tree.nodes.get('Daylight')
        if node is not None and abs(node.outputs[0].default_value - amount) > 1e-6:
            node.outputs[0].default_value = amount


def set_plain_colors(scene):
    """Give every Plain water material the World Exporter's color and biome choice."""
    settings = getattr(scene, 'mcbedrock_world_exporter', None)
    if settings is None:
        return 0
    changed = 0
    for material in bpy.data.materials:
        if material.get(TAG) != 'PLAIN' or material.node_tree is None or material.library:
            continue
        nodes = material.node_tree.nodes
        if nodes.get('Water tint') is not None:
            nodes['Water tint'].outputs[0].default_value = (*settings.plain_water_color, 1)
        if nodes.get('Biome colors') is not None:
            nodes['Biome colors'].outputs[0].default_value = 1. if settings.plain_water_biome else 0.
        material.diffuse_color = (*settings.plain_water_color, 1)
        changed += 1
    return changed


def _objects(scene, collection):
    if (not collection or not collection.get('mcaddon_miex_export') or
            collection not in set(scene.collection.children_recursive)):
        raise ValueError('Choose an imported MiEx world collection')
    if any(other != scene and collection in set(other.collection.children_recursive)
           for other in bpy.data.scenes):
        raise ValueError('This MiEx collection is shared with another scene; make an independent copy first')
    allowed = {collection, *collection.children_recursive}
    shared = {obj for other in bpy.data.scenes if other != scene for obj in other.objects}
    # Objects also linked outside the import, listed once: obj.users_collection
    # scans every collection, per object.
    outside = {obj for c in bpy.data.collections if c not in allowed for obj in c.objects}
    outside.update(obj for other in bpy.data.scenes for obj in other.collection.objects)
    for obj in subtree_objects(collection):
        if obj.type != 'MESH':
            continue
        safe = not (obj.library or obj.override_library or obj in shared or obj in outside)
        yield obj, safe


def _sky_entries(scene):
    """Index the scene once, so large imports do not rescan it per slot."""
    settings = getattr(scene, 'mcbedrock_sky', None)
    entries = {}
    if settings:
        for i, entry in enumerate(settings.materials):
            entries.setdefault((entry.object, entry.index, entry.prepared),
                               (i, entry.original, entry.link))
    return entries


def _discard_unused(materials):
    for material in materials:
        if material and not material.users and material.get(TAG):
            bpy.data.materials.remove(material)


def prepare_atmosphere(scene, material):
    """Join the sky's haze pass without changing water color or surface finish."""
    settings = getattr(scene, 'mcbedrock_sky', None)
    if settings and settings.active and material and (material.get(TAG) or material.get('mcaddon_water_foam') or material.get('mcaddon_water_caustics')):
        from . import atmosphere
        if atmosphere.enabled(scene):
            # A sky reapply receives a new owner/pass name. Remove the two
            # previous AOV nodes before attaching the new scene-owned group.
            owner = settings.owner
            previous = material.get('mcaddon_water_atmosphere')
            if previous != owner and any(
                    other != scene and getattr(other, 'mcbedrock_sky', None) and
                    other.mcbedrock_sky.active and other.mcbedrock_sky.owner == previous
                    for other in bpy.data.scenes):
                return  # A copied/shared material still belongs to a live sky.
            if previous != owner:
                _remove_atmosphere(material)
            atmosphere.prepare_material(scene, material)
            material['mcaddon_water_atmosphere'] = owner


def _remove_atmosphere(material):
    tree = material.node_tree
    if tree:
        for name in ('MC Atmosphere weight', 'MC Atmosphere buffer'):
            node = tree.nodes.get(name)
            if node:
                tree.nodes.remove(node)
    if 'mcaddon_water_atmosphere' in material:
        del material['mcaddon_water_atmosphere']


def clear_atmosphere(scene):
    """Detach only this sky's optional AOV nodes before its groups are removed."""
    settings = getattr(scene, 'mcbedrock_sky', None)
    if settings:
        if any(other != scene and getattr(other, 'mcbedrock_sky', None) and
               other.mcbedrock_sky.active and other.mcbedrock_sky.owner == settings.owner
               for other in bpy.data.scenes):
            return
        for material in bpy.data.materials:
            if (material.get(TAG) or material.get('mcaddon_water_foam') or material.get('mcaddon_water_caustics')) and material.get('mcaddon_water_atmosphere') == settings.owner:
                _remove_atmosphere(material)


def _upgrade_controls(previous, prepared):
    """Keep edited shared controls and retain the old graph for manual recovery."""
    defaults = {'Water tint': (.18, .50, .62, 1), 'Wave strength': .14,
                'Optical thickness': .25}
    if previous.get(TAG) == 'VIBRANT' and previous.get(VERSION_TAG, 0) >= 3:
        # Version 3's pale tint stood in for depth absorption; an untouched
        # one gives way to clear water and the owned body of water.
        defaults = {'Water tint': (.70, .86, .95, 1), 'Wave strength': 1.0,
                    'Animation speed': 1.0}
    if previous.get(TAG) == 'DUNGEONS':
        defaults = {'Water tint': (.003, .07, .20, 1), 'Opacity': 1.0,
                    'Effects strength': .18, 'Effects speed': .05,
                    'Surface fleck strength': 1 / 3, 'Reflection strength': .05}
        if previous.get(VERSION_TAG, 0) >= 3:
            defaults.update({'Water tint': (.001, .06, .08, 1),
                             'Reflection strength': .25, 'Wave strength': .12,
                             'Wave speed': .05, 'Transmission': .6,
                             'Transmission tint': (.25, .85, .65, 1),
                             'Optical thickness': .25})
    for name, default in defaults.items():
        old = previous.node_tree.nodes.get(name)
        new = prepared.node_tree.nodes.get(name)
        if old is None or new is None or not old.outputs or not new.outputs:
            continue
        value = old.outputs[0].default_value
        edited = (any(abs(a - b) > 1e-6 for a, b in zip(value, default))
                  if isinstance(default, tuple) else abs(value - default) > 1e-6)
        if edited:
            new.outputs[0].default_value = value
    previous.use_fake_user = True
    previous['mcaddon_water_previous_shader'] = True
    if not previous.name.endswith(' (previous shader)'):
        previous.name += ' (previous shader)'


def _enable_eevee(scene, preset):
    """Keep native pixel reflections on an explicit, successful water apply."""
    eevee = getattr(scene, 'eevee', None)
    if eevee is None:
        return False
    enabled = not eevee.use_raytracing
    values = {'use_raytracing': True}
    if preset in PRESETS:
        # Spatial reuse and its bilateral blur blend adjacent authored normal
        # cells and block-shaped shoreline reflections. Trace each pixel
        # instead; retain the user's temporal settings.
        values.update({'ray_tracing_options.resolution_scale': '1',
                       'ray_tracing_options.denoise_spatial': False,
                       'ray_tracing_options.denoise_bilateral': False})
    for path, value in values.items():
        target = eevee
        parts = path.split('.')
        for part in parts[:-1]:
            target = getattr(target, part)
        setattr(target, parts[-1], value)
    settings = getattr(scene, 'mcbedrock_sky', None)
    if settings and settings.active:
        # Water intentionally retains its explicit tracing choice on restore.
        # Removing an older sky must not undo that later user action.
        previous = json.loads(settings.previous_eevee or '{}')
        previous.update(values)
        settings.previous_eevee = json.dumps(previous)
    return enabled


WAVES = 'mcaddon_plain_waves'          # collection: Plain water rises and falls like Dungeons II


def waves(collection, preset):
    """Whether this water look swells: always for Dungeons II, by choice for Plain."""
    return preset == 'DUNGEONS' or (preset == 'PLAIN' and bool(collection.get(WAVES, True)))


def set_waves(scene, collection, enabled):
    """Turn Plain water's waves on or off on an import already styled Plain:
    the water and its whitewater line start or stop swelling together."""
    from . import water_foam, water_contact
    collection[WAVES] = bool(enabled)
    preset = collection.get(TAG)
    block = max(.001, float(collection.get('mcaddon_miex_block_units', 1)))
    for obj, safe in _objects(scene, collection):
        if not safe or obj.get(water_foam.TAG):
            continue
        is_water = obj.get(water_contact.TAG) or any(m and is_water_material(m) for m in getattr(obj.data, 'materials', ()))
        if not is_water:
            continue
        if waves(collection, preset):
            water_foam.add_bob(obj, scene, block)
        else:
            water_foam.remove_bob(obj)


def apply(scene, collection, preset, whitewater_style=None):
    """Replace recognized water slots in this import; return changed/skipped counts.

    Object overrides preserve shared mesh data. Objects linked outside the target
    import are skipped. Reapplying a current shader preserves edited nodes.
    Explicit application upgrades older shaders and retains their graph.
    """
    if preset == 'ORIGINAL':
        return restore(scene, collection)
    if preset not in PRESETS:
        raise ValueError('Unknown water preset: ' + str(preset))
    changed = skipped = 0
    cache, discarded, old_sky = {}, set(), set()
    sky_entries, replaced_sky = _sky_entries(scene), set()
    for obj, safe in _objects(scene, collection):
        if not safe:
            skipped += 1
            continue
        for index, slot in enumerate(obj.material_slots):
            current = slot.material
            states = obj.get(STATE)
            state = states.get(str(index)) if states else None
            if state and state.get('prepared') != current:
                # An explicit user slot assignment takes precedence on restore.
                discarded.add(state.get('prepared'))
                del states[str(index)]
                state = None
            if state and state.get('collection') != collection:
                # A duplicated object can carry a valid restoration record from
                # another import. Adopt its original, never mutate its material.
                state = dict(state.items())
            sky_entry = sky_entries.get((obj, index, current))
            original = state.get('original') if state else (sky_entry[1] if sky_entry else current)
            if not is_water_material(original):
                continue
            same_preset = bool(state and current and current.get(TAG) == preset)
            version = VERSIONS[preset]
            upgrade = same_preset and current.get(VERSION_TAG, 0) < version
            if same_preset and not upgrade:
                continue
            cache_key = (original, current) if upgrade else original
            prepared = cache.get(cache_key)
            if prepared is None:
                prepared = build_material(scene, original, preset)
                if upgrade:
                    _upgrade_controls(current, prepared)
                prepare_atmosphere(scene, prepared)
                cache[cache_key] = prepared
            if STATE not in obj:
                obj[STATE] = {}
            link = state['link'] if state else (sky_entry[2] if sky_entry else slot.link)
            obj[STATE][str(index)] = dict(original=original, prepared=prepared,
                                          link=link, collection=collection)
            slot.link = 'OBJECT'
            slot.material = prepared
            if state:
                discarded.add(current)
            if sky_entry:
                replaced_sky.add(sky_entry[0])
                old_sky.add(current)
            changed += 1
    # The lookup holds plain values, not RNA collection entries. Deleting in
    # reverse keeps every saved index valid and leaves unrelated records intact.
    for index in sorted(replaced_sky, reverse=True):
        scene.mcbedrock_sky.materials.remove(index)
    _discard_unused(discarded)
    for material in old_sky:
        if not material.users and material.get('mc_sky_surface_finish'):
            bpy.data.materials.remove(material)
    from . import water_foam, water_caustics
    water_objects = [obj for obj, safe in _objects(scene, collection) if safe and
                     any(slot.material and slot.material.get(TAG) == preset
                         for slot in obj.material_slots)]
    if water_objects:
        collection[TAG] = preset
    raytracing_enabled = _enable_eevee(scene, preset) if water_objects else False
    depth = None
    if water_objects and preset == 'DUNGEONS':
        baked = water_depth.bake(scene, collection, water_objects)
        if baked:
            image, origin_x, origin_y, size_x, size_y, depth = baked
            materials = {slot.material for obj in water_objects for slot in obj.material_slots
                         if slot.material and slot.material.get(TAG) == preset}
            for material in materials:
                nodes = material.node_tree.nodes
                if 'Water depth map' not in nodes:
                    continue  # An older shader kept by the user has no depth blend.
                nodes['Water depth map'].image = image
                for name, amount in (('Depth map origin X', origin_x), ('Depth map origin Y', origin_y),
                                     ('Depth map size X', size_x), ('Depth map size Y', size_y)):
                    nodes[name].outputs[0].default_value = amount
    else:
        water_depth.remove(collection)
    # Vibrant's depth colour comes from an owned body of water instead.
    body = (water_body.rebuild(scene, collection, preset, water_objects=water_objects)
            if water_objects and preset == 'VIBRANT' else None)
    if body is None:
        water_body.remove(collection)
    # Live whitewater around anything placed in the water is part of every
    # water style (it was an opt-in toggle). The whitewater style is
    # remembered on the import for later reapplies.
    if water_contact.LIVE in collection:
        del collection[water_contact.LIVE]
    if whitewater_style is not None:
        collection[water_contact.STYLE] = whitewater_style
    exporter = getattr(scene, 'mcbedrock_world_exporter', None)
    if preset == 'PLAIN' and exporter is not None:
        collection[WAVES] = bool(exporter.plain_water_waves)
    contact = (water_contact.rebuild(scene, collection, preset, water_objects=water_objects)
               if water_objects else None)
    if contact is None:
        water_contact.remove(collection)
    foam = water_foam.rebuild(scene, collection, water_objects=water_objects) if preset == 'DUNGEONS' else None
    if preset != 'DUNGEONS':
        water_foam.remove(collection)
    # Dungeons II's water itself rises and falls with its whitewater, on the
    # same clock, so the surface and the foam swell together. Plain water
    # does too while its Waves are on (remembered on the import).
    block = max(.001, float(collection.get('mcaddon_miex_block_units', 1)))
    for obj, safe in _objects(scene, collection):
        # The whitewater sheets own their bob; only the imported water is ours.
        if not safe or obj.get(water_foam.TAG) or obj.get(water_contact.TAG):
            continue
        if waves(collection, preset) and obj in water_objects:
            water_foam.add_bob(obj, scene, block)
        else:
            water_foam.remove_bob(obj)
    caustics = water_caustics.rebuild(scene, collection, water_objects=water_objects) if preset == 'VIBRANT' else None
    if preset != 'VIBRANT':
        water_caustics.remove(collection)
    return dict(changed=changed, skipped=skipped, foam=foam, caustics=caustics, depth=depth,
                body=body, contact=contact, raytracing_enabled=raytracing_enabled)


def set_whitewater_style(scene, collection, style):
    """Change the whitewater style of an import whose water is already styled.

    Only the whitewater sheet is rebuilt; the water, its baked foam and depth
    stay as they are. Returns the sheet's summary, or None.
    """
    collection[water_contact.STYLE] = style
    preset = collection.get(TAG)
    if preset not in ('DUNGEONS', 'VIBRANT', 'PLAIN'):
        return None
    water_objects = [obj for obj, safe in _objects(scene, collection) if safe and
                     any(slot.material and slot.material.get(TAG) == preset for slot in obj.material_slots)]
    if water_objects:
        return water_contact.rebuild(scene, collection, preset, water_objects=water_objects)
    water_contact.remove(collection)
    return None


def outdated(collection):
    """Plain water saved by an older Sparkle: its shore bake or shader predates this one."""
    if collection.library or collection.get(TAG) != 'PLAIN':
        return False
    if any(int(obj.get(water_contact.TAG, 0)) < water_contact.VERSION for obj in water_contact.owned(collection)):
        return True
    return any(slot.material is not None and slot.material.get(TAG) == 'PLAIN' and
               slot.material.get(VERSION_TAG, 0) < PLAIN_VERSION
               for obj in subtree_objects(collection) for slot in getattr(obj, 'material_slots', ()))


def upgrade(scene):
    """Apply Plain water again on imports saved before its current version.

    Fixes dashed shore lines and drops the old sky reflection once, after a
    file loads. Edited controls carry over as on any explicit reapply.
    Returns how many imports were upgraded."""
    upgraded = 0
    for collection in scene.collection.children_recursive:
        if outdated(collection):
            swell = collection.get(WAVES)
            apply(scene, collection, 'PLAIN')
            if swell is not None and bool(swell) != bool(collection.get(WAVES)):
                set_waves(scene, collection, bool(swell))   # the import's own choice, not the exporter's
            upgraded += 1
    return upgraded


def restore(scene, collection):
    """Restore owned slots; preserve user edits and leave ray tracing enabled."""
    changed = skipped = 0
    discarded = set()
    for obj, safe in _objects(scene, collection):
        states = obj.get(STATE)
        if not states:
            continue
        if not safe:
            skipped += 1
            continue
        for key, state in list(states.items()):
            index = int(key)
            if index < len(obj.material_slots):
                slot = obj.material_slots[index]
                if slot.material == state.get('prepared'):
                    # Restoring a DATA link reveals whatever is on the shared
                    # mesh now; a user's later mesh material edit stays intact.
                    slot.material = state.get('original')
                    slot.link = state.get('link', 'DATA')
                    changed += 1
            discarded.add(state.get('prepared'))
            del states[key]
        if not states:
            del obj[STATE]
    collection[TAG] = 'ORIGINAL'
    from . import water_foam, water_caustics
    water_foam.remove(collection)
    for obj, safe in _objects(scene, collection):
        if safe:
            water_foam.remove_bob(obj)
    water_caustics.remove(collection)
    water_body.remove(collection)
    water_contact.remove(collection)
    _discard_unused(discarded)
    water_depth.remove(collection)
    settings = getattr(scene, 'mcbedrock_sky', None)
    if changed and settings and settings.active and settings.auto_materials:
        from . import sky_materials
        sky_materials.prepare(scene)
    return dict(changed=changed, skipped=skipped)
