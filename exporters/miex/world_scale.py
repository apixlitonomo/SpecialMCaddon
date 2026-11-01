"""Minecraft block units, explicit legacy diagnosis, and camera-free framing."""
import math
from pathlib import Path
import re

import bpy
from mathutils import Matrix, Vector

from .service import MiExError

BLOCK_SCALE = 1.0 / 16.0  # MiEx mesh coordinates are model pixels, not blocks.
VERSION = 1


def subtree_objects(collection):
    """``collection.all_objects`` as a list in the same order, in linear time.

    ``all_objects`` dedupes with a list scan per object. An individual-blocks
    import holds a collection and objects per MiEx prototype (about 100,000 for
    128 x 128 blocks), where that took seconds per call.
    """
    objects = dict.fromkeys(collection.objects)
    for child in collection.children_recursive:
        for obj in child.objects:
            objects.setdefault(obj)
    return list(objects)


def stamp_import(collection, path):
    from pxr import Sdf, Usd, UsdGeom
    # Only the layer metadata: a full open parsed the whole export again
    # (seconds for an individual-blocks export) to read one value.
    stage = Usd.Stage.Open(Sdf.Layer.OpenAsAnonymous(str(path), True))
    collection['mcaddon_miex_export'] = str(Path(path).resolve())
    collection['mcaddon_miex_scale_version'] = VERSION
    collection['mcaddon_miex_block_units'] = 1.0
    collection['mcaddon_miex_usd_meters'] = UsdGeom.GetStageMetersPerUnit(stage)


def imported_collection(context, name=''):
    collections = [c for c in context.scene.collection.children_recursive
                   if c.get('mcaddon_miex_export')]
    if name:
        return next((c for c in collections if c.name == name), None)
    active = context.view_layer.objects.active
    if active:
        matches = [c for c in collections if active.name in {o.name for o in subtree_objects(c)}]
        if len(matches) == 1:
            return matches[0]
    last = context.scene.get('mcaddon_miex_last_collection', '')
    return next((c for c in collections if c.name == last), None) or (
        collections[0] if len(collections) == 1 else None)


def _spans(points):
    return sorted(max(float(p[i]) for p in points) - min(float(p[i]) for p in points)
                  for i in range(3))


def diagnose_scale(collection):
    """Compare source geometry and current hierarchy, never assume all old scenes are wrong.

    Local geometry sizes and world scale are compared separately so translations
    and rotation do not make a manually fixed import appear incorrect. Ambiguous
    matches, edits, animation, and nonuniform scale require a fresh import.
    """
    from pxr import Usd, UsdGeom
    path = Path(collection.get('mcaddon_miex_export', ''))
    if not path.is_file():
        return dict(state='UNKNOWN', message='Source USD is unavailable; scale cannot be verified.')
    try:
        stage = Usd.Stage.Open(str(path))
        if not stage.GetRootLayer().customLayerData.get('MiEx'):
            return dict(state='UNKNOWN', message='Source has no MiEx provenance; scale cannot be verified.')
        if stage.GetPrimAtPath('/world').IsValid():
            stage.GetPrimAtPath('/world').GetVariantSets().GetVariantSet('MiEx_LOD').SetVariantSelection('render')
        sources = {}
        cache = UsdGeom.XformCache()
        for prim in stage.Traverse():
            if not prim.IsA(UsdGeom.Mesh):
                continue
            mesh = UsdGeom.Mesh(prim)
            points_attr = mesh.GetPointsAttr()
            points = points_attr.Get()
            if not points:
                continue
            matrix = cache.GetLocalToWorldTransform(prim)
            scales = [Vector(tuple(matrix[i][:3])).length for i in range(3)]
            sources.setdefault(prim.GetName()[:63], []).append(
                (_spans(points), scales, points_attr.GetNumTimeSamples() > 1))
        ratios = []
        objects = [o for o in subtree_objects(collection) if o.type == 'MESH' and len(o.data.vertices)]
        for obj in objects:
            candidates = sources.get(obj.name) or sources.get(re.sub(r'\.\d{3,}$', '', obj.name))
            if not candidates or len(candidates) != 1:
                return dict(state='UNKNOWN', message='Source mesh matching is ambiguous; use a fresh import to compare.')
            spans, source_scale, animated = candidates[0]
            scale = tuple(abs(v) for v in obj.matrix_world.to_scale())
            if (animated or min(scale) < 1e-9 or min(source_scale) < 1e-9 or
                    max(scale) / min(scale) > 1.001 or max(source_scale) / min(source_scale) > 1.001):
                return dict(state='UNKNOWN', message='Animated or nonuniform geometry needs a fresh import to compare.')
            local = _spans(obj.bound_box)
            for source, current in zip(spans, local):
                if source > 1e-6:
                    ratios.append(current * scale[0] / (source * source_scale[0] * BLOCK_SCALE))
                elif current > 1e-5:
                    return dict(state='UNKNOWN', message='Mesh geometry was edited; scale cannot be verified safely.')
        if not ratios or min(ratios) <= 0 or max(ratios) / min(ratios) > 1.002:
            return dict(state='UNKNOWN', message='Mesh sizes disagree with the source; scale cannot be verified safely.')
        ratio = sum(ratios) / len(ratios)
        if abs(ratio - 1.0) < .001:
            return dict(state='CORRECT', factor=1.0, message='Scale is correct: one block equals one Blender unit.')
        return dict(state='MISMATCH', factor=1.0 / ratio,
                    message=f'One source block currently measures {ratio:.6g} Blender units.',
                    detail=f'Rescale by {1.0 / ratio:.6g}, or import a corrected copy for comparison.')
    except Exception as exc:
        return dict(state='UNKNOWN', message='Could not verify source USD: ' + str(exc))


def collection_bounds(collection):
    points = []
    for obj in subtree_objects(collection):
        if obj.type == 'MESH' and len(obj.data.vertices):
            matrix = obj.matrix_world
            points += [matrix @ Vector(point) for point in obj.bound_box]
    if not points:
        raise MiExError('This imported world has no mesh bounds to frame')
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    return low, high


def rescale_world(context, collection):
    result = diagnose_scale(collection)
    if result['state'] != 'MISMATCH':
        raise MiExError(result['message'])
    objects = set(subtree_objects(collection))
    roots = [obj for obj in objects if obj.parent not in objects]
    if any(obj.parent or obj.constraints or obj.animation_data for obj in roots):
        raise MiExError('This world has a linked or animated root; import a corrected copy instead')
    if any(child not in objects for obj in objects for child in obj.children):
        raise MiExError('Other objects share this hierarchy; import a corrected copy instead')
    low, high = collection_bounds(collection)
    center = (low + high) * .5
    transform = Matrix.Translation(center) @ Matrix.Scale(result['factor'], 4) @ Matrix.Translation(-center)
    for obj in roots:
        obj.matrix_world = transform @ obj.matrix_world
    context.view_layer.update()
    collection['mcaddon_miex_scale_version'] = VERSION
    collection['mcaddon_miex_block_units'] = 1.0
    return result


def frame_world(context, collection, *, show=False):
    """Frame only the destination view; hidden Layout may be prepared after export."""
    if not context.window:
        return False
    window = context.window
    area = context.area if context.area and context.area.type == 'VIEW_3D' else None
    workspace = window.workspace
    if area is None:
        areas = [a for a in window.screen.areas if a.type == 'VIEW_3D']
        if not areas:
            choices = sorted(bpy.data.workspaces, key=lambda w: w.name != 'Layout')
            for candidate in choices:
                if candidate.get('mcaddon_world_exporter_workspace'):
                    continue
                areas = [a for s in candidate.screens for a in s.areas if a.type == 'VIEW_3D']
                if areas:
                    workspace = candidate
                    break
        if not areas:
            return False
        area = max(areas, key=lambda a: a.width * a.height)
    low, high = collection_bounds(collection)
    center = (low + high) * .5
    radius = max((high - low).length * .5, .5)
    space = area.spaces.active
    region = space.region_3d
    if region is None:
        return False
    # Fit a bounding sphere using the narrower viewport angle, including portrait views.
    aspect = max(.1, min(1., area.width / max(area.height, 1)))
    angle = math.atan(16.0 * aspect / max(space.lens, 1.0))
    distance = 1.1 * radius / math.sin(angle)
    region.view_perspective = 'PERSP'
    region.view_location = center
    region.view_distance = distance
    space.clip_start = max(.001, min(.1, radius / 10000.))
    space.clip_end = max(100., distance + radius * 4.)
    if show and workspace != window.workspace:
        window.workspace = workspace
    area.tag_redraw()
    return True
