"""Trailer materials: Element Animation's world look on imported MiEx blocks.

Optional, and fully reversible. Bram Stout describes the world materials of
Element's Minecraft videos (bramstout.nl, Minecraft Live 2024 and the Element
Unreal pipeline): a diffuse texture with "a little rough specular",
vegetation with two-sided illumination for a translucent look, leaves with
subsurface scattering to show their shape, and emissive blocks that glow
brighter than their texture alone. This applies that recipe to the
Principled materials of one imported world.

Every changed input keeps its original value on the shader node, and every
added node is named, so remove() puts the materials back exactly.
"""
import re

import bpy

FLAG = 'mc_world_trailer_materials'    # collection: trailer materials applied
TAG = 'mc_trailer_material'            # material: changed by this module
ORIGINAL = 'mc_trailer_original_'      # shader node: original input values
PREFIX = 'MC Trailer'

SKIP = re.compile(r'water|lava|glass|(?:^|[^a-z])ice|portal|fire|particle')
LEAVES = re.compile(r'leaves|azalea|mangrove_roots|wart_block')
PLANTS = re.compile(r'short_grass|tall_grass|fern|flower|dandelion|poppy|tulip|orchid|allium|oxeye|cornflower|'
                    r'lily_of_the_valley|bush|sapling|vine|petal|wheat|carrots|potatoes|beetroots|sugar_cane|'
                    r'kelp|seagrass|bamboo|roots|sprouts|dripleaf')
GLOWING = re.compile(r'torch|lantern|glowstone|shroomlight|froglight|redstone_lamp|magma|campfire|candle|'
                     r'end_rod|jack_o_lantern|beacon|glow_lichen|amethyst_cluster|crying_obsidian')

ROUGHNESS, SPECULAR = .62, .32          # a little rough specular
LEAF_SCATTER, LEAF_RADIUS, LEAF_SCALE = .3, (1., .55, .25), .06
PLANT_TRANSLUCENCY = .3                 # share of light passing through plants
GLOW = 2.5                              # emissive blocks: emission strength factor


def _names(material):
    tree = material.node_tree
    images = [n.image.name for n in tree.nodes if n.type == 'TEX_IMAGE' and n.image] if tree else []
    return ' '.join([material.name, str(material.get('mcaddon_miex_source', ''))] + images).lower()


def kind(material):
    """'leaves', 'plant', 'glow', 'block' or None for materials left alone."""
    if material is None or material.library or not material.use_nodes or material.node_tree is None:
        return None
    if not any(n.type == 'BSDF_PRINCIPLED' for n in material.node_tree.nodes):
        return None
    names = _names(material)
    if SKIP.search(names) or material.get('mcaddon_water_preset'):
        return None
    if LEAVES.search(names):
        return 'leaves'
    if PLANTS.search(names):
        return 'plant'
    if GLOWING.search(names):
        return 'glow'
    return 'block'


def _set(shader, name, value):
    """Change one unlinked input, keeping its first original value."""
    port = shader.inputs.get(name)
    if port is None or port.is_linked:
        return
    key = ORIGINAL + name
    if key not in shader:
        current = port.default_value
        shader[key] = tuple(current) if hasattr(current, '__len__') else current
    port.default_value = value


def _translucent(material, shader):
    """Mix a Translucent BSDF of the same color under the plant's surface."""
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    if nodes.get(PREFIX + ' translucent') is not None:
        return
    output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
    if output is None or not output.inputs['Surface'].links:
        return
    surface = output.inputs['Surface'].links[0].from_socket
    translucent = nodes.new('ShaderNodeBsdfTranslucent')
    translucent.name = PREFIX + ' translucent'
    translucent.location = (shader.location.x, shader.location.y - 420)
    color = shader.inputs['Base Color']
    if color.links:
        links.new(color.links[0].from_socket, translucent.inputs['Color'])
    else:
        translucent.inputs['Color'].default_value = color.default_value
    mix = nodes.new('ShaderNodeMixShader')
    mix.name = PREFIX + ' mix'
    mix.location = (output.location.x - 200, output.location.y - 160)
    mix.inputs['Fac'].default_value = PLANT_TRANSLUCENCY
    links.new(surface, mix.inputs[1])
    links.new(translucent.outputs[0], mix.inputs[2])
    links.new(mix.outputs[0], output.inputs['Surface'])
    # Cutout plants keep their holes: transparency mixed over the result.
    alpha = shader.inputs['Alpha']
    if alpha.links or alpha.default_value < 1:
        transparent = nodes.new('ShaderNodeBsdfTransparent')
        transparent.name = PREFIX + ' cutout'
        cutout = nodes.new('ShaderNodeMixShader')
        cutout.name = PREFIX + ' cutout mix'
        if alpha.links:
            links.new(alpha.links[0].from_socket, cutout.inputs['Fac'])
        else:
            cutout.inputs['Fac'].default_value = alpha.default_value
        links.new(transparent.outputs[0], cutout.inputs[1])
        links.new(translucent.outputs[0], cutout.inputs[2])
        links.new(cutout.outputs[0], mix.inputs[2])


def apply_material(material):
    what = kind(material)
    if what is None:
        return False
    shaders = [n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED']
    for shader in shaders:
        _set(shader, 'Roughness', ROUGHNESS)
        _set(shader, 'Specular IOR Level', SPECULAR)
        if what == 'leaves':
            _set(shader, 'Subsurface Weight', LEAF_SCATTER)
            _set(shader, 'Subsurface Radius', LEAF_RADIUS)
            _set(shader, 'Subsurface Scale', LEAF_SCALE)
        elif what == 'glow':
            port = shader.inputs.get('Emission Strength')
            lit = port is not None and not port.is_linked and port.default_value > 0
            if lit:
                original = shader.get(ORIGINAL + 'Emission Strength', port.default_value)
                _set(shader, 'Emission Strength', original * GLOW)
    if what == 'plant' and shaders:
        _translucent(material, shaders[0])
    material[TAG] = what
    return True


def remove_material(material):
    if material is None or material.node_tree is None or TAG not in material:
        return False
    tree = material.node_tree
    for shader in (n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED'):
        for key in [k for k in shader.keys() if k.startswith(ORIGINAL)]:
            port = shader.inputs.get(key[len(ORIGINAL):])
            if port is not None and not port.is_linked:
                port.default_value = shader[key]
            del shader[key]
    mix = tree.nodes.get(PREFIX + ' mix')
    if mix is not None:
        source = mix.inputs[1].links[0].from_socket if mix.inputs[1].links else None
        for link in list(mix.outputs[0].links):
            if source is not None:
                tree.links.new(source, link.to_socket)
    for node in [n for n in tree.nodes if n.name.startswith(PREFIX)]:
        tree.nodes.remove(node)
    del material[TAG]
    return True


def materials(scene, collection):
    """The world's own materials, plus Sky & Lighting's copies of them."""
    found = {}
    for obj in collection.all_objects:
        if obj.type != 'MESH':
            continue
        for slot in obj.material_slots:
            if slot.material is not None:
                found[slot.material.as_pointer()] = slot.material
    sky = getattr(scene, 'mcbedrock_sky', None)
    for entry in getattr(sky, 'materials', ()):
        original, prepared = entry.original, entry.prepared
        if original is None or prepared is None:
            continue
        if original.as_pointer() in found or prepared.as_pointer() in found:
            found[original.as_pointer()] = original
            found[prepared.as_pointer()] = prepared
    return list(found.values())


def set_world(scene, collection, enabled):
    """Apply or remove trailer materials on one imported world; returns a status."""
    collection[FLAG] = bool(enabled)
    changed = 0
    for material in materials(scene, collection):
        changed += apply_material(material) if enabled else remove_material(material)
    if enabled:
        return f'trailer materials on {changed} materials' if changed else 'no materials to change'
    return 'original materials restored'
