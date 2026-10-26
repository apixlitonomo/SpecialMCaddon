"""Area preview requests and mesh decoding for the World Exporter's 3D preview, without Blender.

The bridge's mcaddonPreview command is MiEx experimental-28's chunkPreview with
the area's cut sides kept (MCAddonPreview.java). It returns one vertex-coloured
mesh per chunk: quads in 1/16-block units at their world position, wound
counter-clockwise, one normal per face and linear RGB corner colours from each
texture's average colour and biome tint. Decoding runs on the job thread and
hands the UI flat float32/int32 arrays that the GPU takes as they are.
"""
import json
import math

import numpy as np

COMMAND = 'mcaddonPreview'
UNITS = 16.0
# Minecraft's own face shading: tops full, north/south .8, east/west .6, bottoms .5.
SHADE_TOP, SHADE_BOTTOM, SHADE_Z, SHADE_X = 1.0, .5, .8, .6
STEPS = (1, 2, 4, 8, 16)
# Faces per chunk at each block step, with the ground kept at full height detail
# and everything deeper in DEEP_Y-tall cells (measured on a forested 26.3 world:
# 256 chunks at full detail are about 495,000 faces and take about 5 s).
FACES = {1: 2000, 2: 620, 4: 160, 8: 40, 16: 10}
# About 120 MB of GPU and CPU memory; the middle of a large area stays at full detail.
BUDGET = 1_000_000
MAX_CHUNKS = 16384
DEEP_Y = 8
# Blocks below a chunk's lowest surface column still drawn at surface detail.
SURFACE_MARGIN = 6
# Faces per request, so each response stays a few megabytes.
BATCH_FACES = 40_000
MAX_BATCH = 128
# Neutral linear grey for faces MiEx sends without colours.
FALLBACK_COLOR = .2


def area_chunks(min_x, max_x, min_z, max_z, excluded=()):
    """Chunk coordinates covering an inclusive block area, minus excluded chunks."""
    skipped = {tuple(p) for p in excluded}
    return [(cx, cz) for cz in range(min_z >> 4, (max_z >> 4) + 1)
            for cx in range(min_x >> 4, (max_x >> 4) + 1) if (cx, cz) not in skipped]


def plan(count):
    """(core step, core chunks, outer step) for an area of `count` chunks, or None when too large.

    The coarsest step is the finest one the face budget allows for every chunk;
    the chunks nearest the middle get the next finer step for whatever budget is left.
    """
    if count > MAX_CHUNKS:
        return None
    for i, step in enumerate(STEPS):
        if count * FACES[step] <= BUDGET:
            if i == 0:
                return step, count, step
            finer = STEPS[i - 1]
            core = (BUDGET - count * FACES[step]) // (FACES[finer] - FACES[step])
            return finer, int(min(count, core)), step
    return STEPS[-1], 0, STEPS[-1]


def lod_for(count):
    """The coarsest block step used for an area of `count` chunks, or None when too large."""
    steps = plan(count)
    return steps[2] if steps else None


def batch_size(step):
    """Chunks per request at a block step."""
    return max(1, min(MAX_BATCH, BATCH_FACES // FACES[step]))


def surface_floor(tile):
    """The lowest filled column of a decoded map tile (rgba, heights), or None."""
    if not tile or not tile[1]:
        return None
    rgba, heights = tile
    values = [h for h, a in zip(heights, rgba[3::4]) if a]
    return min(values) if values else None


def _local(lo, hi, origin, step):
    """Inclusive world range [lo, hi] as an exclusive chunk-local range aligned to step."""
    start = max(0, lo - origin) // step * step
    end = min(16, -(-min(16, hi + 1 - origin) // step) * step)
    return start, end


def _aligned(lo, hi, step):
    """Inclusive [lo, hi] widened to whole cells of `step`, as [start, end)."""
    return math.floor(lo / step) * step, -(-(hi + 1) // step) * step


def chunk_rows(area, steps, chunks=None, surface=None):
    """[(chunk, step, rows)] for an area, nearest its centre first.

    `area` is (min_x, max_x, min_y, max_y, min_z, max_z) with inclusive bounds and
    `steps` comes from plan(). Rows carry explicit world offsets (upstream
    otherwise offsets every chunk by its chunk index) and the whole area in world
    blocks, outside which the bridge treats blocks as air so the cut sides show.
    Where `surface` knows a chunk's lowest surface column, the ground keeps full
    height detail and everything more than SURFACE_MARGIN below it is drawn in
    DEEP_Y-tall cells, as a second row for the same chunk.
    """
    min_x, max_x, min_y, max_y, min_z, max_z = area
    if chunks is None:
        chunks = area_chunks(min_x, max_x, min_z, max_z)
    core_step, core_count, outer_step = steps
    mid_x = (min_x + max_x + 1) / 2
    mid_z = (min_z + max_z + 1) / 2
    order = sorted(chunks, key=lambda c: (c[0] * 16 + 8 - mid_x) ** 2 + (c[1] * 16 + 8 - mid_z) ** 2)
    surface = surface or {}
    entries = []
    for index, (cx, cz) in enumerate(order):
        step = core_step if index < core_count else outer_step
        x0, x1 = _local(min_x, max_x, cx * 16, step)
        z0, z1 = _local(min_z, max_z, cz * 16, step)
        if x1 <= x0 or z1 <= z0:
            continue
        area_x, area_z = _aligned(min_x, max_x, step), _aligned(min_z, max_z, step)
        ground = 1 if step <= 2 else step
        deep = max(DEEP_Y, step)
        low = _aligned(min_y, max_y, deep)[0]
        high = _aligned(min_y, max_y, ground)[1]
        base = dict(chunkX=cx, chunkZ=cz, minX=x0, maxX=x1, minZ=z0, maxZ=z1, lodSize=step,
                    worldOffsetX=0, worldOffsetY=0, worldOffsetZ=0, singleMeshNoTextures=True,
                    areaMinX=area_x[0], areaMaxX=area_x[1], areaMinY=low, areaMaxY=high,
                    areaMinZ=area_z[0], areaMaxZ=area_z[1])
        top = surface.get((cx, cz))
        if top is None or deep <= ground:
            span = _aligned(min_y, max_y, ground)
            rows = [dict(base, minY=span[0], maxY=span[1], lodYSize=ground, areaMinY=span[0])]
        else:
            # Whole deep cells only, so none reaches above Max Y.
            cut = max(low, min(high // deep * deep, math.floor((top - SURFACE_MARGIN) / deep) * deep))
            rows = []
            if cut > low:
                rows.append(dict(base, minY=low, maxY=cut, lodYSize=deep))
            if high > cut:
                rows.append(dict(base, minY=cut, maxY=high, lodYSize=ground))
        entries.append(((cx, cz), step, rows))
    return entries


def row_key(rows):
    return json.dumps(rows, sort_keys=True)


def shading(normals):
    """Minecraft face brightness for unit normals; tilted faces blend between sides."""
    n = normals / np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-6)
    up = np.where(n[:, 1] > 0, SHADE_TOP, SHADE_BOTTOM)
    return (n[:, 0] ** 2 * SHADE_X + n[:, 1] ** 2 * up + n[:, 2] ** 2 * SHADE_Z).astype(np.float32)


def _ints(values):
    return np.asarray(values if values is not None else (), dtype=np.int64)


def decode_mesh(mesh):
    """(positions (n, 3) in blocks, shaded linear colours (n, 3), triangles (m, 3)) or None.

    Every MiEx preview face is a quad with four corners, so each face becomes
    two triangles over its own four vertices.
    """
    faces = _ints(mesh.get('faceIndices'))
    count = len(faces) // 4
    vertices = np.asarray(mesh.get('vertices') or (), dtype=np.float32)
    vertices = vertices[:len(vertices) // 3 * 3].reshape(-1, 3)
    if not count or not len(vertices):
        return None
    faces = faces[:count * 4]
    if faces.min() < 0 or faces.max() >= len(vertices):
        return None
    positions = vertices[faces] / np.float32(UNITS)
    normals = np.asarray(mesh.get('normals') or (), dtype=np.float32)
    normals = normals[:len(normals) // 3 * 3].reshape(-1, 3)
    normal_index = _ints(mesh.get('normalIndices'))
    valid_normals = len(normals) and len(normal_index) and normal_index.min() >= 0 and normal_index.max() < len(normals)
    if valid_normals and len(normal_index) == count:
        corner_normals = np.repeat(normals[normal_index], 4, axis=0)
    elif valid_normals and len(normal_index) >= count * 4:
        corner_normals = normals[normal_index[:count * 4]]
    else:
        quads = positions.reshape(-1, 4, 3)
        corner_normals = np.repeat(np.cross(quads[:, 1] - quads[:, 0], quads[:, 2] - quads[:, 0]), 4, axis=0)
    colors = np.asarray(mesh.get('colors') or (), dtype=np.float32)
    colors = colors[:len(colors) // 3 * 3].reshape(-1, 3)
    color_index = _ints(mesh.get('colorIndices'))
    if (len(colors) and len(color_index) >= count * 4
            and color_index[:count * 4].min() >= 0 and color_index[:count * 4].max() < len(colors)):
        corner_colors = colors[color_index[:count * 4]]
    else:
        corner_colors = np.full((count * 4, 3), FALLBACK_COLOR, dtype=np.float32)
    corner_colors = np.clip(corner_colors * shading(corner_normals)[:, None], 0, None).astype(np.float32)
    base = np.arange(count, dtype=np.int32) * 4
    triangles = np.stack((base, base + 1, base + 2, base, base + 2, base + 3), axis=1).reshape(-1, 3)
    return np.ascontiguousarray(positions, dtype=np.float32), corner_colors, np.ascontiguousarray(triangles)


def merge(parts):
    """One (positions, colours, triangles, bounds) from several decoded meshes, or None."""
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    offsets = np.cumsum([0] + [len(p[0]) for p in parts[:-1]])
    positions = np.concatenate([p[0] for p in parts])
    colors = np.concatenate([p[1] for p in parts])
    triangles = np.concatenate([p[2] + np.int32(o) for p, o in zip(parts, offsets)])
    bounds = (tuple(float(v) for v in positions.min(axis=0)), tuple(float(v) for v in positions.max(axis=0)))
    return positions, colors, triangles.astype(np.int32), bounds


def decode_chunks(rows):
    """{(chunkX, chunkZ): (positions, colours, triangles, bounds)} for a preview response.

    A chunk can come back more than once (its ground and the simplified part
    below); those merge. Chunks without faces are left out; MiEx also skips
    chunks the world lacks.
    """
    parts = {}
    for row in rows or ():
        try:
            key = int(row['chunkX']), int(row['chunkZ'])
        except (KeyError, TypeError, ValueError):
            continue
        merged = merge([decode_mesh(mesh) for mesh in row.get('meshes') or ()])
        if merged is not None:
            parts.setdefault(key, []).append(merged)
    return {key: found[0] if len(found) == 1 else merge(found) for key, found in parts.items()}
