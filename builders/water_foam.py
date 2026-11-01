"""Square contact whitewater, baked from actual water/solid intersections.

The mesh is rebuilt only when the user applies Dungeons water. No frame handler,
geometry modifier or screen-space depth pass is needed. Solid faces crossing the
water and adjacent bank tops seed foam, never water chunk edges alone. Reapply
after moving a shoreline or prop. Generated datablocks are owned and removable.
"""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from functools import lru_cache
import math

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from ..exporters.miex.world_scale import subtree_objects

TAG = 'mcaddon_water_foam'
BOB_HEIGHT = 'mc_bob_height'      # object: how far the foam rises above its rest, in blocks
BOB_SECONDS = 'mc_bob_seconds'    # object: one rise and fall, in seconds
BOB = (1 / 16, 2.4)               # Dungeons II: one texture pixel, a slow swell
VERSION = 3
CELL = .0625
WIDTH_CELLS = 2
LIFT = .006
MAX_WATER_FACES = 250_000
MAX_CONTACT_FACES = 500_000
MAX_PATCHES = 120_000
MAX_CANDIDATES = 600_000


def _shared_collection(collection):
    return sum(collection in set(scene.collection.children_recursive)
               for scene in bpy.data.scenes) > 1


def remove(collection):
    """Remove only foam belonging to this import, including orphaned resources."""
    if _shared_collection(collection):
        return 0
    removed = 0
    materials = set()
    for obj in subtree_objects(collection):
        if not obj.get(TAG):
            continue
        # A user-linked copy elsewhere must retain its objects and materials.
        allowed = {collection, *collection.children_recursive}
        if any(c not in allowed for c in obj.users_collection):
            for c in list(obj.users_collection):
                if c in allowed:
                    c.objects.unlink(obj)
            continue
        mesh = obj.data
        materials.update(m for m in mesh.materials if m)
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.users == 0 and mesh.get(TAG):
            bpy.data.meshes.remove(mesh)
        removed += 1
    for material in materials:
        if not material.users and material.get(TAG):
            bpy.data.materials.remove(material)
    for key in ('mcaddon_water_foam_patches', 'mcaddon_water_foam_truncated',
                'mcaddon_water_foam_reason'):
        if key in collection:
            del collection[key]
    return removed


def _intersection(points, level):
    """Intersect a convex mesh face with a horizontal water plane."""
    hits = []
    for a, b in zip(points, (*points[1:], points[0])):
        da, db = a.z - level, b.z - level
        if abs(da) < 1e-5:
            hits.append(a)
        if da * db < 0:
            hits.append(a.lerp(b, da / (da - db)))
    if len(hits) < 2:
        return None
    # Vertex coincidences and coplanar edges may produce duplicate hits.
    a = hits[0]
    b = max(hits, key=lambda p: (p - a).length_squared)
    a = max(hits, key=lambda p: (p - b).length_squared)
    return (a, b) if (a - b).length_squared > 1e-9 else None


def _cells(a, b, normal, cell):
    """Rasterize a continuous fixed-width outline with square corner joins."""
    tangent = Vector((b.x - a.x, b.y - a.y))
    length = tangent.length
    if length < 1e-6:
        return
    tangent /= length
    width = WIDTH_CELLS * cell
    if abs(tangent.x) < 1e-6 or abs(tangent.y) < 1e-6:
        # Minecraft shores are predominantly axis-aligned. Enumerate their thin
        # strip directly instead of checking 49 neighbors at every line sample.
        origin = Vector((a.x, a.y))
        corners = [origin + tangent * along + normal * outward
                   for along in (-width, length + width) for outward in (0, width)]
        positions = ((x, y)
                     for x in range(math.floor(min(p.x for p in corners) / cell),
                                    math.floor(max(p.x for p in corners) / cell) + 1)
                     for y in range(math.floor(min(p.y for p in corners) / cell),
                                    math.floor(max(p.y for p in corners) / cell) + 1))
    else:
        steps = max(1, math.ceil(length / (cell * .75)))
        samples = (a.lerp(b, step / steps) for step in range(steps + 1))
        positions = ((x, y) for p in samples
                     for x in range(math.floor(p.x / cell) - 3, math.floor(p.x / cell) + 4)
                     for y in range(math.floor(p.y / cell) - 3, math.floor(p.y / cell) + 4))
    seen = set()
    for x, y in positions:
        if (x, y) in seen:
            continue
        seen.add((x, y))
        dx, dy = (x + .5) * cell - a.x, (y + .5) * cell - a.y
        along = dx * tangent.x + dy * tangent.y
        outward = dx * normal.x + dy * normal.y
        # Extend by the strip width at both ends for square joins. No
        # noise, stippling, bevel or radial distance rounds the corners.
        if -width <= along <= length + width and .01 * cell < outward <= width:
            yield x, y


def _bank_edges(points):
    """Outward edges of an imported bank top whose underwater sides were culled."""
    center = sum(points, Vector()) / len(points)
    for a, b in zip(points, (*points[1:], points[0])):
        outward = Vector((b.y - a.y, a.x - b.x))
        if outward.length_squared < 1e-12:
            continue
        # Do not depend on winding after an import's negative-scale transform.
        if outward.dot(Vector(((a.x + b.x) / 2 - center.x,
                               (a.y + b.y) / 2 - center.y))) < 0:
            outward.negate()
        yield a, b, outward.normalized()


def add_bob(obj, scene, block=1.):
    """Dungeons II's whitewater rises and falls with the waves, keeping its width.

    A driver on the object's delta Z, on the scene clock like the water's own
    animation, always above its rest height so it never sinks into the water.
    A plain expression, so it runs without Python auto-run and in every
    render. The object's mc_bob_height and mc_bob_seconds tune it.
    """
    obj[BOB_HEIGHT] = BOB[0] * block
    obj[BOB_SECONDS] = BOB[1]
    obj.driver_remove('delta_location', 2)
    driver = obj.driver_add('delta_location', 2).driver
    driver.type = 'SCRIPTED'
    driver.expression = 'height * (0.5 - 0.5 * cos(6.283185 * frame * fps_base / (fps * max(seconds, 0.01))))'
    for name, kind, target, path in (('height', 'OBJECT', obj, f'["{BOB_HEIGHT}"]'),
                                     ('seconds', 'OBJECT', obj, f'["{BOB_SECONDS}"]'),
                                     ('fps', 'SCENE', scene, 'render.fps'),
                                     ('fps_base', 'SCENE', scene, 'render.fps_base')):
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'SINGLE_PROP'
        variable.targets[0].id_type = kind
        variable.targets[0].id = target
        variable.targets[0].data_path = path


def remove_bob(obj):
    """Undo add_bob: the object back at rest, its driver and settings gone."""
    if BOB_HEIGHT not in obj:
        return
    obj.driver_remove('delta_location', 2)
    obj.delta_location.z = 0.
    for key in (BOB_HEIGHT, BOB_SECONDS):
        if key in obj:
            del obj[key]


def _material(scene):
    material = bpy.data.materials.new('Sparkle · Dungeons square whitewater')
    material[TAG] = VERSION
    material['mc_edge_exclude'] = True
    material.use_nodes = True
    material.diffuse_color = (.88, .96, 1.0, 1)
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    output = nodes.new('ShaderNodeOutputMaterial')
    surface = nodes.new('ShaderNodeBsdfPrincipled')
    surface.name = 'Whitewater surface'
    surface.inputs['Base Color'].default_value = material.diffuse_color
    surface.inputs['Roughness'].default_value = 1
    surface.inputs['Specular IOR Level'].default_value = 0
    links.new(surface.outputs['BSDF'], output.inputs['Surface'])
    from .water import prepare_atmosphere
    prepare_atmosphere(scene, material)
    return material


def prepare_atmosphere(scene):
    """Attach new/reapplied sky haze without running the surface-finish pipeline."""
    from .water import prepare_atmosphere as prepare_material
    materials = {slot.material for obj in scene.objects if obj.get(TAG)
                 for slot in obj.material_slots if slot.material and slot.material.get(TAG)}
    for material in materials:
        prepare_material(scene, material)


def _root(objects):
    roots = set()
    for obj in objects:
        root = obj
        while root.parent:
            root = root.parent
        roots.add(root)
    return next(iter(roots)) if len(roots) == 1 else None


def _tiles(xmin, xmax, ymin, ymax, size):
    return ((x, y) for x in range(math.floor(xmin / size), math.floor(xmax / size) + 1)
            for y in range(math.floor(ymin / size), math.floor(ymax / size) + 1))


def _rectangles(patches, block):
    """Coalesce the same square-cell footprint, preserving its hard corners."""
    rows = {}
    for (level, x, y), z in patches.items():
        # Import rotations leave harmless float noise on horizontal surfaces.
        rows.setdefault((level, round(z / block, 4), y), []).append(x)
    runs = {}
    for (level, z, y), xs in rows.items():
        xs.sort()
        start = previous = xs[0]
        for x in xs[1:] + [xs[-1] + 2]:
            if x != previous + 1:
                runs.setdefault((level, z, start, previous + 1), []).append(y)
                start = x
            previous = x
    for (_, z, xmin, xmax), ys in runs.items():
        ys.sort()
        start = previous = ys[0]
        for y in ys[1:] + [ys[-1] + 2]:
            if y != previous + 1:
                yield xmin, xmax, start, previous + 1, z * block
                start = y
            previous = y


def _subtract(rect, cut):
    """Difference of two integer-cell rectangles, without rounding corners."""
    x0, x1, y0, y1 = rect
    a, b, c, d = max(x0, cut[0]), min(x1, cut[1]), max(y0, cut[2]), min(y1, cut[3])
    if a >= b or c >= d:
        return [rect]
    return [r for r in ((x0, a, y0, y1), (b, x1, y0, y1),
                         (a, b, y0, c), (a, b, d, y1)) if r[0] < r[1] and r[2] < r[3]]


def _union_rectangles(rectangles):
    """Sweep integer runs into disjoint quads; cost depends on edges, not pixels."""
    planes = defaultdict(list)
    for x0, x1, y0, y1, z in rectangles:
        planes[z].append((x0, x1, y0, y1))
    for z, rects in planes.items():
        events = defaultdict(list)
        for x0, x1, y0, y1 in rects:
            events[y0].append((x0, x1, 1))
            events[y1].append((x0, x1, -1))
        active, pending = Counter(), {}
        ys = sorted(events)
        for index, y in enumerate(ys):
            for x0, x1, delta in events[y]:
                active[x0, x1] += delta
                if not active[x0, x1]:
                    del active[x0, x1]
            spans = []
            for x0, x1 in sorted(active):
                if spans and x0 <= spans[-1][1]:
                    spans[-1] = (spans[-1][0], max(spans[-1][1], x1))
                else:
                    spans.append((x0, x1))
            spans = set(spans)
            for span in pending.keys() - spans:
                yield *span, pending.pop(span), y, z
            if index + 1 < len(ys):
                for span in spans:
                    pending.setdefault(span, y)


def _axis_region(a, b, normal, cell):
    """Integer-cell strip for a Minecraft axis-aligned contact segment."""
    dx, dy = b.x - a.x, b.y - a.y
    if not ((abs(dx) < 1e-5 and abs(normal.y) < 1e-5) or
            (abs(dy) < 1e-5 and abs(normal.x) < 1e-5)):
        return None
    vertical = abs(dx) < 1e-5
    cross = a.x if vertical else a.y
    outward = normal.x if vertical else normal.y
    lo, hi = sorted((a.y, b.y) if vertical else (a.x, b.x))
    # Match _cells' fixed width, including square joins at both ends.
    width = WIDTH_CELLS * cell
    along0 = math.ceil((lo - width) / cell - .5 - 1e-6)
    along1 = math.floor((hi + width) / cell - .5 + 1e-6) + 1
    low, high = sorted((cross + outward * .01 * cell, cross + outward * width))
    cross0 = math.ceil(low / cell - .5 - 1e-6)
    cross1 = math.floor(high / cell - .5 + 1e-6) + 1
    if cross0 >= cross1 or along0 >= along1:
        return ()
    return ((cross0, cross1, along0, along1) if vertical else
            (along0, along1, cross0, cross1))


def _water_bins(footprint, block):
    bins = {(x, y, z) for (x, y), levels in footprint.items() for level in levels
            for z in range(math.floor(level / block - 1e-4),
                           math.floor(level / block + .25 + 1e-4) + 1)}
    origin = np.min(tuple(bins), axis=0)
    span = np.max(tuple(bins), axis=0) - origin + 1
    codes = np.array([((x - origin[0]) * span[1] + y - origin[1]) * span[2] + z - origin[2]
                      for x, y, z in bins], dtype=np.int64)
    return origin, span, codes


def _contact_indices(obj, water_slots, imported, spatial_bins, tile_size, block):
    """Bulk-reject dry faces before Python transforms millions of mesh corners."""
    mesh = obj.data
    count = len(mesh.polygons)
    if not count:
        return ()
    coordinates = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get('co', coordinates)
    matrix = np.asarray(obj.matrix_world, dtype=np.float64)
    points = coordinates.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
    indices = np.empty(len(mesh.loops), dtype=np.int32)
    starts = np.empty(count, dtype=np.int32)
    mesh.loops.foreach_get('vertex_index', indices)
    mesh.polygons.foreach_get('loop_start', starts)
    corners = points[indices]
    low = np.minimum.reduceat(corners, starts, axis=0)
    high = np.maximum.reduceat(corners, starts, axis=0)
    normals = np.empty(count * 3, dtype=np.float32)
    mesh.polygons.foreach_get('normal', normals)
    normal_matrix = np.asarray(obj.matrix_world.to_3x3().inverted_safe().transposed())
    normals = normals.reshape(-1, 3) @ normal_matrix.T
    lengths = np.linalg.norm(normals, axis=1)
    nz = normals[:, 2] / np.maximum(lengths, 1e-20)
    keep = (np.abs(nz) <= .95) | ((nz > .95) & imported)
    if water_slots:
        materials = np.empty(count, dtype=np.int32)
        mesh.polygons.foreach_get('material_index', materials)
        keep &= ~np.isin(materials, tuple(water_slots))
    selected = np.flatnonzero(keep)
    if not len(selected):
        return ()
    low, high = low[selected], high[selected]
    # Typical MiEx faces span at most one block. Check all eight endpoint bins;
    # long merged/user faces bypass this rejection and retain exact clipping.
    bins0 = np.floor(low / (tile_size, tile_size, block)).astype(np.int64)
    bins1 = np.floor(high / (tile_size, tile_size, block)).astype(np.int64)
    close = np.any(bins1 - bins0 > 1, axis=1)
    origin, span, codes = spatial_bins
    def encode(x, y, z):
        return ((x - origin[0]) * span[1] + y - origin[1]) * span[2] + z - origin[2]
    for x in (bins0[:, 0], bins1[:, 0]):
        for y in (bins0[:, 1], bins1[:, 1]):
            for z in (bins0[:, 2], bins1[:, 2]):
                close |= np.isin(encode(x, y, z), codes,
                                 kind='table' if int(np.prod(span)) <= 8_000_000 else None)
    return selected[close]


def rebuild(scene, collection, *, water_objects=None):
    """Return {patches, truncated}; make contact foam for this import's water.

    Reads original mesh geometry in world space. Contact objects can be anywhere
    in the scene, including a separate prop inserted through an uncut water
    plane. Modifier/deformation results are intentionally not baked. Work and
    output are bounded; a truncated result is explicitly recorded and returned.
    """
    from .water import is_water_material
    from .water_caustics import _opaque

    if not collection or not collection.get('mcaddon_miex_export'):
        raise ValueError('Choose an imported MiEx world collection')
    if collection not in set(scene.collection.children_recursive):
        raise ValueError('The imported world is not in this scene')
    if _shared_collection(collection):
        raise ValueError('This world is shared by multiple scenes; make a separate copy before changing water')
    block = max(.001, float(collection.get('mcaddon_miex_block_units', 1)))
    cell, ray_height = CELL * block, .05 * block
    # Source-water surfaces sit about .115 blocks below a neighboring bank top.
    # Only this narrow band can stand in for a culled submerged block side.
    bank_drop = .25 * block
    members = subtree_objects(collection)
    imported_objects = set(members)
    sources = list(water_objects) if water_objects is not None else members
    vertices, faces, levels, participating = [], [], set(), []
    footprint, water_rects, irregular = {}, defaultdict(list), set()
    tile_size = 8 * block
    truncated = False
    reason = ''
    for obj in sources:
        if obj.type != 'MESH' or obj.hide_render or obj.get(TAG):
            continue
        slots = {i for i, slot in enumerate(obj.material_slots) if is_water_material(slot.material)}
        if not slots:
            continue
        matrix = obj.matrix_world
        normal_matrix = matrix.to_3x3().inverted_safe().transposed()
        used = False
        for poly in obj.data.polygons:
            if poly.material_index not in slots or (normal_matrix @ poly.normal).normalized().z < .95:
                continue
            points = [matrix @ obj.data.vertices[i].co for i in poly.vertices]
            low, high = min(p.z for p in points), max(p.z for p in points)
            # Foam sits flat; sloping waterfalls are handled by the water shader.
            if high - low > .025 * block:
                continue
            offset = len(vertices)
            vertices.extend(points)
            faces.append(tuple(range(offset, len(vertices))))
            level = round(sum(p.z for p in points) / len(points) / block, 4) * block
            levels.add(level)
            x0, x1 = min(p.x for p in points), max(p.x for p in points)
            y0, y1 = min(p.y for p in points), max(p.y for p in points)
            axis = (len(points) == 4 and high - low < .0001 * block and
                    all(min(abs(p.x - x0), abs(p.x - x1)) < .0001 * block and
                        min(abs(p.y - y0), abs(p.y - y1)) < .0001 * block for p in points))
            for tile in _tiles(x0 - cell, x1 + cell, y0 - cell, y1 + cell, tile_size):
                footprint.setdefault(tile, set()).add(level)
                if axis:
                    water_rects[tile, level].append((x0, x1, y0, y1))
                else:
                    irregular.add((tile, level))
            used = True
            if len(faces) >= MAX_WATER_FACES:
                truncated = True
                reason = 'Water face limit'
                break
        if used:
            participating.append(obj)
        if truncated:
            break
    if not faces:
        remove(collection)
        return dict(patches=0, cells=0, truncated=truncated, reason=reason)
    water = BVHTree.FromPolygons(vertices, faces, all_triangles=False)
    # Join tessellated water before quantizing it. Otherwise a water seam at a
    # fractional grid position can leave a false one-cell gap in the outline.
    for key, rects in water_rects.items():
        water_rects[key] = [r[:4] for r in _union_rectangles((*r, 0) for r in rects)]

    @lru_cache(maxsize=4096)
    def nearby_water(tiles, level):
        rects = {rect for tile in tiles for rect in water_rects.get((tile, level), ())}
        return tuple(r[:4] for r in _union_rectangles((*r, 0) for r in rects))

    levels = sorted(levels)
    spatial_bins = _water_bins(footprint, block)
    xmin, xmax = min(p.x for p in vertices), max(p.x for p in vertices)
    ymin, ymax = min(p.y for p in vertices), max(p.y for p in vertices)
    candidates, patches, strips = set(), {}, set()
    scanned = 0
    stop = False
    solid_materials = {}
    for obj in scene.objects:
        if obj.type != 'MESH' or obj.hide_render or obj.get(TAG) or obj.get('mc_sky_owner'):
            continue
        matrix = obj.matrix_world
        bounds = [matrix @ Vector(p) for p in obj.bound_box]
        if (max(p.x for p in bounds) < xmin - cell or min(p.x for p in bounds) > xmax + cell or
                max(p.y for p in bounds) < ymin - cell or min(p.y for p in bounds) > ymax + cell or
                max(p.z for p in bounds) < levels[0] - .001 * block or
                min(p.z for p in bounds) > levels[-1] + bank_drop):
            continue
        water_slots = {i for i, slot in enumerate(obj.material_slots) if is_water_material(slot.material)}
        for i, slot in enumerate(obj.material_slots):
            material = slot.material
            if material not in solid_materials:
                solid_materials[material] = _opaque(material)
            if not solid_materials[material]:
                water_slots.add(i)
        if obj.material_slots and len(water_slots) == len(obj.material_slots):
            continue
        normal_matrix = matrix.to_3x3().inverted_safe().transposed()
        for index in _contact_indices(obj, water_slots, obj in imported_objects, spatial_bins, tile_size, block):
            poly = obj.data.polygons[int(index)]
            if poly.material_index in water_slots:
                continue
            normal = (normal_matrix @ poly.normal).normalized()
            bank_top = normal.z > .95 and obj in imported_objects
            if abs(normal.z) > .95 and not bank_top:
                continue
            points = [matrix @ obj.data.vertices[i].co for i in poly.vertices]
            pxmin, pxmax = min(p.x for p in points), max(p.x for p in points)
            pymin, pymax = min(p.y for p in points), max(p.y for p in points)
            if (pxmax < xmin - cell or pxmin > xmax + cell or
                    pymax < ymin - cell or pymin > ymax + cell):
                continue
            nearby = set()
            for tile in _tiles(pxmin - cell, pxmax + cell, pymin - cell, pymax + cell, tile_size):
                nearby.update(footprint.get(tile, ()))
            local_levels = sorted(nearby)
            low, high = min(p.z for p in points), max(p.z for p in points)
            if bank_top:
                if high - low > 1e-5 * block:
                    continue
                lo, hi = bisect_left(local_levels, low - bank_drop), bisect_left(local_levels, low - 1e-5 * block)
            else:
                normal = Vector((normal.x, normal.y)).normalized()
                lo, hi = bisect_left(local_levels, low - 1e-5 * block), bisect_right(local_levels, high + 1e-5 * block)
            if lo == hi:
                continue
            # Hilltops, undersides and faces above/below all water levels must
            # not spend the contact budget before a later shoreline is visited.
            scanned += 1
            if scanned > MAX_CONTACT_FACES:
                truncated = stop = True
                reason = 'Contact face limit'
                break
            for level in local_levels[lo:hi]:
                if bank_top:
                    segments = _bank_edges(points)
                else:
                    segment = _intersection(points, level)
                    segments = [(*segment, normal)] if segment else []
                for a, b, outward in segments:
                    region = _axis_region(a, b, outward, cell)
                    tiles = list(_tiles(min(a.x, b.x) - cell * 2, max(a.x, b.x) + cell * 2,
                                        min(a.y, b.y) - cell * 2, max(a.y, b.y) + cell * 2, tile_size))
                    if region is not None and not any((tile, level) in irregular for tile in tiles):
                        if not region:
                            continue
                        nearby_rects = nearby_water(tuple(tiles), level)
                        wet, inland = [], []
                        sx, sy = round(outward.x * WIDTH_CELLS), round(outward.y * WIDTH_CELLS)
                        for x0, x1, y0, y1 in nearby_rects:
                            # The same .001-cell inset used by the ray fallback.
                            clipped = (max(region[0], math.ceil(x0 / cell - .001)),
                                       min(region[1], math.floor(x1 / cell + .001)),
                                       max(region[2], math.ceil(y0 / cell - .001)),
                                       min(region[3], math.floor(y1 / cell + .001)))
                            if clipped[0] < clipped[1] and clipped[2] < clipped[3]:
                                wet.append(clipped)
                            if bank_top:
                                inland.append((math.ceil(x0 / cell - .5) + sx,
                                               math.floor(x1 / cell - .5) + 1 + sx,
                                               math.ceil(y0 / cell - .5) + sy,
                                               math.floor(y1 / cell - .5) + 1 + sy))
                        # Water on the bank's inland side means a seam or a
                        # floating top, not shoreline. Subtract whole runs.
                        for cut in inland:
                            wet = [part for rect in wet for part in _subtract(rect, cut)]
                            if not wet:
                                break
                        z = round((level + LIFT * block) / block, 4) * block
                        strips.update((*rect, z) for rect in wet)
                        if len(strips) + len(candidates) > MAX_CANDIDATES:
                            truncated = stop = True
                            reason = 'Contact region limit'
                            break
                        continue
                    for x, y in _cells(a, b, outward, cell):
                        key = (level, x, y)
                        if key in candidates:
                            continue
                        center = Vector(((x + .5) * cell, (y + .5) * cell, level + ray_height))
                        hit, _, _, _ = water.ray_cast(center, Vector((0, 0, -1)), ray_height * 2)
                        if hit is None:
                            continue
                        if bank_top:
                            # An adjacent bank requires water to end on its land
                            # side. A floating top over uncut water, a shared water
                            # chunk seam and an unsupported export edge cannot pass.
                            inland = center - Vector((outward.x * cell, outward.y * cell, 0)) * WIDTH_CELLS
                            if water.ray_cast(inland, Vector((0, 0, -1)), ray_height * 2)[0] is not None:
                                continue
                        candidates.add(key)
                        if len(strips) + len(candidates) > MAX_CANDIDATES:
                            truncated = stop = True
                            reason = 'Contact region limit'
                            break
                        # Require all corners to be on water so no tile hangs over an
                        # exported edge. Tiny inset makes exact shared borders robust.
                        covered = True
                        for dx, dy in ((-.499, -.499), (.499, -.499), (.499, .499), (-.499, .499)):
                            corner = center + Vector((dx * cell, dy * cell, 0))
                            if water.ray_cast(corner, Vector((0, 0, -1)), ray_height * 2)[0] is None:
                                covered = False
                                break
                        if covered:
                            patches[key] = hit.z + LIFT * block
                    if stop:
                        break
                if stop:
                    break
            if stop:
                break
        if stop:
            break
    # Finish computation before replacing existing owned data.
    remove(collection)
    strips.update(_rectangles(patches, block))
    cells = 0
    if strips:
        verts, quads = [], []
        for xmin, xmax, ymin, ymax, z in _union_rectangles(strips):
            if len(quads) >= MAX_PATCHES:
                truncated = True
                reason = 'Outline quad limit'
                break
            offset = len(verts)
            cells += (xmax - xmin) * (ymax - ymin)
            verts.extend(((xmin * cell, ymin * cell, z), (xmax * cell, ymin * cell, z),
                          (xmax * cell, ymax * cell, z), (xmin * cell, ymax * cell, z)))
            quads.append((offset, offset + 1, offset + 2, offset + 3))
        mesh = bpy.data.meshes.new('Sparkle square whitewater')
        mesh[TAG] = VERSION
        mesh.from_pydata(verts, [], quads)
        mesh.update()
        obj = bpy.data.objects.new('Sparkle · Shore and object whitewater', mesh)
        obj[TAG] = VERSION
        obj['mc_sky_owner'] = 'water_foam'
        obj.hide_select = True
        collection.objects.link(obj)
        root = _root(participating)
        if root and root not in participating:
            obj.parent = root
            obj.matrix_world = Matrix.Identity(4)
        mesh.materials.append(_material(scene))
        add_bob(obj, scene, block)
    count = len(quads) if strips else 0
    collection['mcaddon_water_foam_patches'] = count
    collection['mcaddon_water_foam_truncated'] = truncated
    collection['mcaddon_water_foam_reason'] = reason
    return dict(patches=count, cells=cells, contact_faces=scanned, contact_regions=len(strips),
                ray_cells=len(candidates), water_faces=len(faces), truncated=truncated, reason=reason)
