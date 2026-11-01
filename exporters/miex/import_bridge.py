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
import time
import bpy
import numpy as np
from . import individual_blocks
from .service import MiExError
from .world_scale import BLOCK_SCALE, stamp_import, frame_world, subtree_objects

DATA_KINDS = ('objects', 'collections', 'meshes', 'pointclouds', 'curves', 'cameras', 'lights',
              'materials', 'node_groups', 'images', 'actions')

# Seconds per stage of the latest import_world call, read by the perf checks:
# usd_import is Blender's own importer; the rest is Sparkle's post-processing.
LAST_TIMINGS = {}
# The latest Individual-blocks merge (individual_blocks.merge): prototypes MiEx wrote,
# distinct models, models kept and seconds; 'error' when it fell back to the unmerged file.
LAST_MERGE = {}


class _Stages:
    def __init__(self):
        self.start = self.last = time.perf_counter()
        self.seconds = {}

    def __call__(self, stage):
        now = time.perf_counter()
        self.seconds[stage] = self.seconds.get(stage, 0.0) + now - self.last
        self.last = now

    def publish(self):
        LAST_TIMINGS.clear()
        LAST_TIMINGS.update({k: round(v, 4) for k, v in self.seconds.items()})
        LAST_TIMINGS['total'] = round(time.perf_counter() - self.start, 4)


def _roll_back(before):
    """Remove everything the failed import added.

    One batch_remove remaps users once. Removing a 64 x 64 individual-blocks
    import (10,000 objects, 9,000 collections) ID by ID took 470 s.
    """
    added = [item for kind in DATA_KINDS for item in getattr(bpy.data, kind) if item not in before[kind]]
    try:
        bpy.data.batch_remove(added)
    except (TypeError, RuntimeError, ReferenceError):
        pass
    for kind in DATA_KINDS:
        data = getattr(bpy.data, kind)
        for item in set(data) - before[kind]:
            try:
                data.remove(item, do_unlink=True)
            except (TypeError, RuntimeError):
                if item.users == 0:
                    data.remove(item)


def _name_models(merged, objects):
    """Name each merged model's collection after its block: ``017 oak_door``.

    Blender calls them ``proto_017``; Collection Info hands its children to
    Instance on Points in name order, so the zero-padded number stays first. The
    instancer is recognised by its exact ``proto_index`` values.
    """
    for obj in objects:
        attribute = obj.data.attributes.get('proto_index') if obj.type == 'POINTCLOUD' else None
        if attribute is None or attribute.data_type != 'INT' or not len(attribute.data):
            continue
        values = np.empty(len(attribute.data), np.int32)
        attribute.data.foreach_get('value', values)
        labels = [labels for labels, indices in merged.instancers.values() if np.array_equal(indices, values)]
        if len(labels) != 1:
            continue
        labels = labels[0]
        for modifier in obj.modifiers:
            group = getattr(modifier, 'node_group', None)
            info = next((n for n in group.nodes if n.bl_idname == 'GeometryNodeCollectionInfo'), None) if group else None
            parent = info.inputs['Collection'].default_value if info else None
            numbered = [(re.fullmatch(r'proto_(\d+)(?:\.\d+)?', child.name), child)
                        for child in (parent.children if parent else ())]
            if len(numbered) != len(labels) or not all(match for match, _ in numbered):
                continue
            numbered.sort(key=lambda item: int(item[0].group(1)))
            if [int(match.group(1)) for match, _ in numbered] != list(range(len(labels))):
                continue
            for index, (match, child) in enumerate(numbered):
                child.name = match.group(1) + ' ' + labels[index]


def import_world(context, filepath, label='World', *, water_preset='ORIGINAL', outlines=False,
                 whitewater_style='MATCH', merge_models=True):
    """Import a MiEx export; ``merge_models`` merges repeated Individual-blocks models first."""
    from pxr import Usd
    from .materials import SetupMaterials
    from ...builders import water
    stage = _Stages()
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
    collection = merged = None
    LAST_MERGE.clear()
    stage('prepare')
    try:
        Usd.Stage.SetGlobalVariantFallbacks({'MiEx_LOD': ['render']})
        collection = bpy.data.collections.new('MiEx - ' + label)
        context.scene.collection.children.link(collection)
        context.view_layer.update()
        context.view_layer.active_layer_collection = context.view_layer.layer_collection.children[collection.name]
        stage('prepare')
        if merge_models:
            try:
                merged = individual_blocks.merge(path)
            except Exception as exc:  # A layout MiEx did not write this way imports unmerged.
                LAST_MERGE['error'] = str(exc)
                print('Sparkle: importing individual blocks unmerged:', exc)
            if merged:
                LAST_MERGE.update(prototypes=merged.prototypes, models=merged.models, kept=merged.kept,
                                  seconds=round(merged.seconds, 4))
            stage('merge')
        result = bpy.ops.wm.usd_import(filepath=str(merged.path if merged else path), import_visible_only=False,
            set_frame_range=False, scale=BLOCK_SCALE, apply_unit_conversion_scale=False,
            mtl_name_collision_mode='MAKE_UNIQUE',
            import_proxy=False)
        stage('usd_import')
        if result != {'FINISHED'}:
            raise MiExError('Blender could not import the MiEx USD')
        new_objects = set(bpy.data.objects) - before['objects']
        if not new_objects:
            raise MiExError('The exported selection contains no objects')
        if merged:
            _name_models(merged, new_objects)
        new_materials = set(bpy.data.materials) - before['materials']
        # Not collection.all_objects: it dedupes with a list scan per object, which
        # took seconds for an individual-blocks import (a collection per prototype).
        contained_objects = set(subtree_objects(collection))
        stage('new_data')
        flattened_meshes = set()
        # Keep all newly imported hierarchy under the result collection.
        for obj in new_objects:
            if obj not in contained_objects:
                collection.objects.link(obj)
            if obj.type == 'MESH':
                mesh = obj.data
                # Write once per shared mesh in C instead of one RNA update per face.
                if mesh not in flattened_meshes:
                    mesh.polygons.foreach_set('use_smooth', array('b', [0]) * len(mesh.polygons))
                    flattened_meshes.add(mesh)
                # MiExImport's displayColor correction for Material solid shading.
                # Restrict it to this import so existing scene materials stay intact.
                colors = mesh.attributes.get('displayColor')
                if colors and len(colors.data) and len(mesh.materials):
                    material = mesh.materials[0]
                    if material in new_materials:
                        color = colors.data[0].color
                        material.diffuse_color = (
                            *(max(0.0, channel) ** (1.0 / 2.2) for channel in color[:3]), 1.0)
        stage('objects')
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
        stage('materials')
        # MiEx exports model pixels (16 per block), even when USD meter metadata
        # differs. Assets and schematics use block units independent of scene units.
        stamp_import(collection, path)
        # Sparkle outlines skip worlds that did not opt in.
        collection['mc_world_outlines'] = bool(outlines)
        stage('stamp')
        if water_preset != 'ORIGINAL':
            water.apply(context.scene, collection, water_preset, whitewater_style=whitewater_style)
            stage('water')
        context.scene['mcaddon_miex_last_collection'] = collection.name
        # Evaluates the new scene once (the viewport reuses it); framing needs world matrices.
        context.view_layer.update()
        stage('evaluate')
        frame_world(context, collection)
        stage('frame')
        return collection
    except Exception:
        _roll_back(before)
        stage('rollback')
        raise
    finally:
        if merged:
            try:
                merged.path.unlink()
            except OSError:
                pass
        Usd.Stage.SetGlobalVariantFallbacks(variants)
        context.view_layer.active_layer_collection = previous_collection
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in previous_selection:
            if obj.name in context.view_layer.objects:
                obj.select_set(True)
        if previous_active and previous_active.name in context.view_layer.objects:
            context.view_layer.objects.active = previous_active
        stage('restore')
        stage.publish()
