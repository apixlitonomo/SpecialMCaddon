"""Per-block water depth map for the Dungeons II shallow/deep color blend.

The game's M_SpicewoodWater blends its Colour Shallow and Colour Deep
parameters by the scene depth behind the translucent surface. Blender surface
shaders cannot read that depth, so we raycast once from every exposed water
block and store the distance to the bed in a small image sampled by world XY.
Imported geometry and mesh data stay untouched.
"""
import math

import bpy
from mathutils import Vector

TAG = 'mcaddon_water_depth'
MAX_DEPTH = 32  # blocks; the encoded range of the 8-bit map
MAX_TEXELS = 4096
MAX_RAY_STEPS = 16
DOWN = Vector((0, 0, -1))


def remove(collection):
    for image in [i for i in bpy.data.images if i.get(TAG) == collection.name_full]:
        if not image.users:
            bpy.data.images.remove(image)


def bake(scene, collection, water_objects):
    """Return (image, origin_x, origin_y, size_x, size_y, report) in world units, or None."""
    from .water import is_water_material
    block = max(.001, float(collection.get('mcaddon_miex_block_units', 1)))
    epsilon = .001 * block
    faces = []
    for obj in water_objects:
        slots = {i for i, slot in enumerate(obj.material_slots) if is_water_material(slot.material)}
        matrix = obj.matrix_world
        normals = matrix.to_3x3().inverted_safe().transposed()
        vertices = obj.data.vertices
        for face in obj.data.polygons:
            if face.material_index not in slots or (normals @ face.normal).normalized().z < .95:
                continue
            points = [matrix @ vertices[i].co for i in face.vertices]
            faces.append((min(p.x for p in points), max(p.x for p in points), min(p.y for p in points),
                          max(p.y for p in points), sum(p.z for p in points) / len(points)))
    from .column_rays import ColumnRays, cells, grid_shift
    # One texel per block of the world's own grid (MiEx centres blocks on whole numbers).
    sx, sy = grid_shift([(face[0], face[2]) for face in faces], block)
    tops = {}
    for low_x, high_x, low_y, high_y, level in faces:
        for x in cells(low_x, high_x, block, sx):
            for y in cells(low_y, high_y, block, sy):
                # Overlapping chunks or stacked pools: the top surface is seen.
                if tops.get((x, y), -math.inf) < level:
                    tops[(x, y)] = level
    if not tops:
        return None
    xs = [k[0] for k in tops]
    ys = [k[1] for k in tops]
    x0, y0 = min(xs) - 1, min(ys) - 1  # one dry border texel on each side
    width, height = max(xs) - x0 + 2, max(ys) - y0 + 2
    stride = max(1, math.ceil(max(width, height) / MAX_TEXELS))
    columns, rows = math.ceil(width / stride), math.ceil(height / stride)

    view_layer = bpy.context.view_layer if bpy.context.scene == scene else scene.view_layers[0]
    depsgraph = view_layer.depsgraph
    depsgraph.update()
    rays = ColumnRays(scene, depsgraph, tops, block, MAX_DEPTH * block + 2 * block, upward=False, shift=(sx, sy))
    water_slots = {}

    def bed(origin, distance):
        start, remaining = origin.copy(), distance
        for _ in range(MAX_RAY_STEPS):
            found, location, _normal, index, obj, _matrix = rays.ray_cast(
                depsgraph, start, DOWN, distance=remaining)
            if not found:
                return None
            original = obj.original
            skip = (original.hide_render or original.get('mcaddon_water_caustics') or
                    original.get('mcaddon_water_foam') or original.get('mc_sky_owner'))
            if not skip:
                if original not in water_slots:
                    evaluated = original.evaluated_get(depsgraph)
                    water_slots[original] = (evaluated, {i for i, slot in enumerate(evaluated.material_slots)
                                                         if is_water_material(slot.material)})
                evaluated, slots = water_slots[original]
                polygons = getattr(evaluated.data, 'polygons', None)
                if polygons is None or not 0 <= index < len(polygons) or polygons[index].material_index not in slots:
                    return origin.z - location.z
            remaining -= (location - start).length + epsilon
            if remaining <= 0:
                return None
            start = location + DOWN * epsilon
        return None

    texels = {}
    traced = 0
    # Every column is traced: the same water gets the same depth map on any computer.
    truncated = False
    for (x, y), level in tops.items():
        key = ((x - x0) // stride, (y - y0) // stride)
        center = Vector(((x + sx + .5) * block, (y + sy + .5) * block, level - epsilon))
        depth = bed(center, MAX_DEPTH * block)
        depth = MAX_DEPTH if depth is None else depth / block
        traced += 1
        texels[key] = max(texels.get(key, 0), depth)

    name = 'Sparkle water depth · ' + collection.name
    image = next((i for i in bpy.data.images if i.get(TAG) == collection.name_full), None)
    if image is not None and tuple(image.size) != (columns, rows):
        bpy.data.images.remove(image)
        image = None
    if image is None:
        image = bpy.data.images.new(name, columns, rows, alpha=False, float_buffer=False)
        image[TAG] = collection.name_full
    image.colorspace_settings.name = 'Non-Color'
    pixels = [0.] * (columns * rows * 4)
    for index in range(3, len(pixels), 4):
        pixels[index] = 1.
    for (cx, cy), depth in texels.items():
        value = min(depth, MAX_DEPTH) / MAX_DEPTH
        offset = (cy * columns + cx) * 4
        pixels[offset] = pixels[offset + 1] = pixels[offset + 2] = value
    image.pixels.foreach_set(pixels)
    image.update()
    image.pack()
    report = dict(blocks=len(tops), traced=traced, texels=len(texels), stride=stride, truncated=truncated)
    return image, (x0 + sx) * block, (y0 + sy) * block, columns * stride * block, rows * stride * block, report
