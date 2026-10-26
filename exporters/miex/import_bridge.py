"""Adapt MiExImport 0.1.9 with scoped materials and import rollback.

Upstream: BramStoutProductions/MiEx, revision
9258afcc507289a9b41c098623bb67312de4dc3d,
extras/Blender/BlenderAddon/MiExImport.py.
Authors: michael212345, Bram Stout, Mahid Sheikh.
BSD-3-Clause; see LICENSE-MiEx.txt in this directory.
"""
import json
from array import array
from pathlib import Path
import re
import bpy
from .service import MiExError
from .world_scale import BLOCK_SCALE, stamp_import, frame_world

DATA_KINDS = ('objects', 'collections', 'meshes', 'curves', 'cameras', 'lights',
              'materials', 'node_groups', 'images', 'actions')


def import_world(context, filepath, label='World', *, water_preset='ORIGINAL', outlines=False, whitewater_style='MATCH'):
    from pxr import Usd
    from .materials import SetupMaterials
    from ...builders import water
    path = Path(filepath)
    sidecar = path.with_name(path.stem + '_materials.json')
    if not path.is_file() or not sidecar.is_file():
        raise MiExError('Export requires both the USD and its _materials.json file')
    with sidecar.open(encoding='utf-8') as stream:
        definitions = json.load(stream)
    before = {kind: set(getattr(bpy.data, kind)) for kind in DATA_KINDS}
    previous_selection = list(context.selected_objects)
    previous_active = context.view_layer.objects.active
    previous_collection = context.view_layer.active_layer_collection
    variants = Usd.Stage.GetGlobalVariantFallbacks()
    collection = None
    try:
        Usd.Stage.SetGlobalVariantFallbacks({'MiEx_LOD': ['render']})
        collection = bpy.data.collections.new('MiEx - ' + label)
        context.scene.collection.children.link(collection)
        context.view_layer.update()
        context.view_layer.active_layer_collection = context.view_layer.layer_collection.children[collection.name]
        result = bpy.ops.wm.usd_import(filepath=str(path), import_visible_only=False,
            set_frame_range=False, scale=BLOCK_SCALE, apply_unit_conversion_scale=False,
            mtl_name_collision_mode='MAKE_UNIQUE',
            import_proxy=False)
        if result != {'FINISHED'}:
            raise MiExError('Blender could not import the MiEx USD')
        new_objects = set(bpy.data.objects) - before['objects']
        if not new_objects:
            raise MiExError('The exported selection contains no objects')
        new_materials = set(bpy.data.materials) - before['materials']
        contained_objects = set(collection.all_objects)
        flattened_meshes = set()
        # Keep all newly imported hierarchy under the result collection.
        for obj in new_objects:
            if obj not in contained_objects:
                collection.objects.link(obj)
            if obj.type == 'MESH':
                # Write once per shared mesh in C instead of one RNA update per face.
                if obj.data not in flattened_meshes:
                    obj.data.polygons.foreach_set('use_smooth', array('b', [0]) * len(obj.data.polygons))
                    flattened_meshes.add(obj.data)
                # MiExImport's displayColor correction for Material solid shading.
                # Restrict it to this import so existing scene materials stay intact.
                colors = obj.data.attributes.get('displayColor')
                if (colors and len(colors.data) and len(obj.data.materials)
                        and obj.data.materials[0] in new_materials):
                    color = colors.data[0].color
                    obj.data.materials[0].diffuse_color = (
                        *(max(0.0, channel) ** (1.0 / 2.2) for channel in color[:3]), 1.0)
        for material in new_materials:
            candidates = [(key, value) for key, value in definitions.items()
                          if material.name == key[:63] or
                          (re.search(r'\.\d{3}$', material.name) and
                           material.name[:-4] == key[:len(material.name) - 4])]
            exact = [pair for pair in candidates if material.name == pair[0][:63]]
            candidates = exact or candidates
            if len(candidates) != 1:
                raise MiExError('Cannot uniquely match MiEx material: ' + material.name)
            SetupMaterials(material, candidates[0][1], str(path.parent),
                           {'max_animation_frames': 1000}).setup_materials()
            water.mark_imported(material, candidates[0][0])
        # MiEx exports model pixels (16 per block), even when USD meter metadata
        # differs. Assets and schematics use block units independent of scene units.
        stamp_import(collection, path)
        # Sparkle outlines skip worlds that did not opt in.
        collection['mc_world_outlines'] = bool(outlines)
        if water_preset != 'ORIGINAL':
            water.apply(context.scene, collection, water_preset, whitewater_style=whitewater_style)
        context.scene['mcaddon_miex_last_collection'] = collection.name
        context.view_layer.update()
        frame_world(context, collection)
        return collection
    except Exception:
        for kind in DATA_KINDS:
            data = getattr(bpy.data, kind)
            for item in set(data) - before[kind]:
                try:
                    data.remove(item, do_unlink=True)
                except (TypeError, RuntimeError):
                    if item.users == 0:
                        data.remove(item)
        raise
    finally:
        Usd.Stage.SetGlobalVariantFallbacks(variants)
        context.view_layer.active_layer_collection = previous_collection
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in previous_selection:
            if obj.name in context.view_layer.objects:
                obj.select_set(True)
        if previous_active and previous_active.name in context.view_layer.objects:
            context.view_layer.objects.active = previous_active
