"""Per-block water depth map for the Dungeons II shallow/deep color blend.

The game's M_SpicewoodWater blends its Colour Shallow and Colour Deep
parameters by the scene depth behind the translucent surface. Blender surface
shaders cannot read that depth, so we raycast once from every exposed water
block and store the distance to the bed in a small image sampled by world XY.
Imported geometry and mesh data stay untouched.
"""
import math
import time

import bpy
from mathutils import Vector

TAG = 'mcaddon_water_depth'
MAX_DEPTH = 32  # blocks; the encoded range of the 8-bit map
MAX_TEXELS = 4096
MAX_RAY_STEPS = 16
MAX_SECONDS = 30.
UNTRACED = 8  # blocks; water left untraced by the time limit reads fairly deep
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
    tops = {}
    for obj in water_objects:
        slots = {i for i, slot in enumerate(obj.material_slots) if is_water_material(slot.material)}
        matrix = obj.matrix_world
        normals = matrix.to_3x3().inverted_safe().transposed()
        vertices = obj.data.vertices
        for face in obj.data.polygons:
            if face.material_index not in slots or (normals @ face.normal).normalized().z < .95:
                continue
            points = [matrix @ vertices[i].co for i in face.vertices]
            level = sum(p.z for p in points) / len(points)
            for x in range(math.floor(min(p.x for p in points) / block + 1e-4),
                           math.ceil(max(p.x for p in points) / block - 1e-4)):
                for y in range(math.floor(min(p.y for p in points) / block + 1e-4),
                               math.ceil(max(p.y for p in points) / block - 1e-4)):
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
    water_slots = {}

    def bed(origin, distance):
        start, remaining = origin.copy(), distance
        for _ in range(MAX_RAY_STEPS):
            found, location, _normal, index, obj, _matrix = scene.ray_cast(
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
    truncated = False
    deadline = time.perf_counter() + MAX_SECONDS
    for (x, y), level in tops.items():
        key = ((x - x0) // stride, (y - y0) // stride)
        if time.perf_counter() >= deadline:
            truncated = True
            texels.setdefault(key, UNTRACED)
            continue
        center = Vector(((x + .5) * block, (y + .5) * block, level - epsilon))
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
    return image, x0 * block, y0 * block, columns * stride * block, rows * stride * block, report
