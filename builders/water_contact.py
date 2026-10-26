"""Live contact whitewater over an import's styled water.

The baked Dungeons whitewater (water_foam.py) only sees what was in the
import when the style was applied. This layer is traced every render, so a
character, boat or prop placed or moved in the water gets white water around
it at once, in both water styles. A few short horizontal rays leave each
surface pixel just above the water line with Blender's Raycast node (5.1+;
real rays in Cycles, screen-space rays in EEVEE). The nearest hit decides how
much foam a pixel gets; foam is cut on the same 1/16 block grid as the
textures, with a ragged edge, so it reads as Minecraft pixels.

The two water styles foam differently. Dungeons II gets that hard, square,
pixel outline, matching its baked whitewater. Vibrant Visuals gets soft,
natural froth: a smooth falloff from the object, broken up by fine noise and
never fully opaque, so the clear water still reads through it. Either can
instead use Simple whitewater: one plain white outline of constant width,
straight along shores with sharp, square corners, no pixels and no froth.
Plain water always gets that outline. It traces no rays: what stands in the
water is measured where it crosses the water line, the world's shores once
when the style is applied and everything else (characters, boats, props)
whenever it moves, poses or the frame changes, so the band is exact and the
same in Cycles and EEVEE.

The foam lives on its own owned sheet, a copy of the water tops lifted a hair
above them, rather than in the water shader. EEVEE decides a dithered
surface's coverage in a pass that cannot trace rays, so foam mixed into the
water itself never showed there; the sheet is blended, which can. Built when a
water style is applied, like the body of water. Older Blender versions skip it.
"""
import math
import re

import bpy
import numpy as np
from mathutils import Matrix

TAG = 'mcaddon_water_contact'
LIVE = 'mcaddon_live_whitewater'  # old imports: opted in when live whitewater was a toggle (removed on reapply)
GROUP = 'MC Water contact foam'
BRUSH_GROUP = 'MC Water square whitewater'
BANK = .3             # blocks above the water a bank top may sit with its sides left out
BOX_ROWS = (.002, .25, .5, .75, .998)   # where the bake's rays cross each block
EMPTY_BOX = (1., 0., 1., 0.)           # x1 < x0: nothing solid in this block
MAX_SHORE_TRIANGLES = 2_000_000
BRUSH_LIFT = .04      # blocks above the water line where solids are measured
COVER = .01           # share of a block a water face must cover to count for it
# Square brush: tops rising more than this across a block (flowing water stepping
# down a waterfall) get no shore line; a flat band over a slope floated above it.
LEVEL_RISE = .25
# Per sheet face, for each of the 3x3 blocks around it, the box (x0, x1, y0,
# y1) that solids fill there at the water line, in that block's 0-1 space:
# the world's shores (baked) and, packed with them, the props (live), which
# get their own, thinner band.
SHORE_BOX = 'mc_shore_box_'
FOAM_BOX = 'mc_foam_box_'
FOAM_CELL = 'mc_foam_cell'           # sheet face: its block's lower corner
PACK = 1024                          # FOAM_BOX value: shore * PACK + prop, each in 1/1023 steps
# Band width around characters, boats and props, per style (blocks).
PROP_WIDTH = {'SIMPLE': .08, 'PLAIN': .08}
NEIGHBOURS = tuple((dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
SKIP_OWNERS = ('mc_sky_owner', 'mc_fog_planes')   # Sparkle's own helpers are not props
VERSION = 18          # 18: shore boxes keep their corner rows; level water only (17: water plants drawn as # planes are not shores)
MARKER = 'mc_water_contact_version'
COLOR = (.88, .96, 1., 1.)
FOAM_ALBEDO = 1.6    # Plain whitewater: light it reflects, as a share of a white block's
ALLOWED = 'mc_live_foam'   # sheet face: 1 where live foam may form, 0 at the world's own plants
# The world's water plants. Some exports draw seagrass and kelp as upright
# # planes, not the diagonal crosses the plan-normal test recognises, and
# their planes cross the water line: without this every clump got a square.
WATER_PLANTS = re.compile(r'seagrass|kelp|sea_pickle|lily_pad|waterlily|bubble|coral(?!_block)')


def _plant_slots(obj):
    """Material slot indices of the object that are water plants."""
    if WATER_PLANTS.search(obj.name.lower()):
        return list(range(max(1, len(obj.material_slots))))
    found = []
    for index, slot in enumerate(obj.material_slots):
        material = slot.material
        if material is not None and WATER_PLANTS.search((material.name + ' ' + str(material.get('mcaddon_miex_source', ''))).lower()):
            found.append(index)
    return found
RAYS = 8
LIFT = .004  # blocks above the water surface
DEFAULTS = (('Foam width', .2), ('Foam amount', 1.), ('Foam lift', .04), ('Foam breakup', .6), ('Pixel foam', 1.),
            ('Plain outline', 0.))
STYLE = 'mcaddon_whitewater_style'   # collection: 'MATCH' (the water's own foam) or 'SIMPLE'
# Per style: (width, amount, breakup, pixel foam, plain outline).
STYLES = {'DUNGEONS': (.2, 1., .6, 1., 0.), 'VIBRANT': (.16, .85, .8, 0., 0.), 'SIMPLE': (.12, 1., 0., 0., 1.),
          # Plain water: a thin clean white band along shores and around props.
          'PLAIN': (.12, 1., 0., 0., 1.)}


def supported():
    return hasattr(bpy.types, 'ShaderNodeRaycast')


def _math(nodes, links, operation, a, b=None, c=None):
    result = nodes.new('ShaderNodeMath')
    result.operation = operation
    for index, source in enumerate((a, b, c)):
        if source is None:
            continue
        if isinstance(source, (int, float)):
            result.inputs[index].default_value = source
        else:
            links.new(source, result.inputs[index])
    return result.outputs[0]


def _build(tree):
    tree.nodes.clear()
    for socket in list(tree.interface.items_tree):
        tree.interface.remove(socket)
    for name, amount in DEFAULTS:
        item = tree.interface.new_socket(name, in_out='INPUT', socket_type='NodeSocketFloat')
        item.default_value = amount
    tree.interface.new_socket('Foam', in_out='OUTPUT', socket_type='NodeSocketFloat')
    nodes, links = tree.nodes, tree.links
    group_in = nodes.new('NodeGroupInput')
    group_out = nodes.new('NodeGroupOutput')
    width, amount, lift, breakup, pixel, plain = (group_in.outputs[name] for name, _ in DEFAULTS)
    geometry = nodes.new('ShaderNodeNewGeometry')
    # Pixel centers on the 1/16 block grid, so foam comes in whole pixels.
    grid = nodes.new('ShaderNodeVectorMath')
    grid.operation = 'SNAP'
    links.new(geometry.outputs['Position'], grid.inputs[0])
    grid.inputs[1].default_value = (1 / 16, 1 / 16, 1e-6)
    center = nodes.new('ShaderNodeVectorMath')
    center.operation = 'ADD'
    links.new(grid.outputs[0], center.inputs[0])
    center.inputs[1].default_value = (1 / 32, 1 / 32, 0)
    # Vibrant traces from the exact point, so its froth has smooth edges.
    sample = nodes.new('ShaderNodeMix')
    sample.data_type = 'VECTOR'
    links.new(pixel, sample.inputs['Factor'])
    links.new(geometry.outputs['Position'], sample.inputs['A'])
    links.new(center.outputs[0], sample.inputs['B'])
    raise_by = nodes.new('ShaderNodeCombineXYZ')
    links.new(lift, raise_by.inputs['Z'])
    start = nodes.new('ShaderNodeVectorMath')
    start.operation = 'ADD'
    links.new(sample.outputs['Result'], start.inputs[0])
    links.new(raise_by.outputs[0], start.inputs[1])
    nearest = width
    for index in range(RAYS):
        angle = 2 * math.pi * (index + .5) / RAYS
        ray = nodes.new('ShaderNodeRaycast')
        links.new(start.outputs[0], ray.inputs['Position'])
        # Cycles reads Direction only when it is linked.
        direction = nodes.new('ShaderNodeCombineXYZ')
        direction.inputs['X'].default_value = math.cos(angle)
        direction.inputs['Y'].default_value = math.sin(angle)
        links.new(direction.outputs[0], ray.inputs['Direction'])
        links.new(width, ray.inputs['Length'])
        # A miss counts as a hit at the full width.
        reach = _math(nodes, links, 'SUBTRACT', 1, ray.outputs['Is Hit'])
        distance = _math(nodes, links, 'MULTIPLY_ADD', reach, width,
                         _math(nodes, links, 'MULTIPLY', ray.outputs['Is Hit'], ray.outputs['Hit Distance']))
        nearest = _math(nodes, links, 'MINIMUM', nearest, distance)
    closeness = _math(nodes, links, 'SUBTRACT', 1, _math(nodes, links, 'DIVIDE', nearest,
                                                          _math(nodes, links, 'MAXIMUM', width, 1e-4)))
    noise = nodes.new('ShaderNodeTexWhiteNoise')
    noise.noise_dimensions = '3D'
    links.new(center.outputs[0], noise.inputs['Vector'])
    ragged = _math(nodes, links, 'MULTIPLY_ADD', _math(nodes, links, 'SUBTRACT', noise.outputs['Value'], .5),
                   breakup, closeness)
    square = _math(nodes, links, 'GREATER_THAN', ragged, .4)
    # Soft froth: fine noise breaks a smooth falloff into bubbles and lace.
    froth = nodes.new('ShaderNodeTexNoise')
    froth.noise_dimensions = '3D'
    froth.inputs['Scale'].default_value = 14
    froth.inputs['Detail'].default_value = 4
    froth.inputs['Roughness'].default_value = .6
    links.new(geometry.outputs['Position'], froth.inputs['Vector'])
    lace = _math(nodes, links, 'MULTIPLY_ADD', _math(nodes, links, 'SUBTRACT', froth.outputs['Fac'], .5),
                 _math(nodes, links, 'MULTIPLY', breakup, 1.6), closeness)
    soft = nodes.new('ShaderNodeMapRange')
    soft.interpolation_type = 'SMOOTHSTEP'
    links.new(lace, soft.inputs['Value'])
    soft.inputs['From Min'].default_value = .3
    soft.inputs['From Max'].default_value = .8
    style = nodes.new('ShaderNodeMix')
    style.data_type = 'FLOAT'
    links.new(pixel, style.inputs['Factor'])
    links.new(soft.outputs['Result'], style.inputs['A'])
    links.new(square, style.inputs['B'])
    foam = style.outputs['Result']
    # Simple whitewater: a plain band of constant width with a smooth,
    # antialiased edge (traced from the exact point, never noise).
    outline = nodes.new('ShaderNodeMapRange')
    outline.interpolation_type = 'SMOOTHSTEP'
    links.new(closeness, outline.inputs['Value'])
    outline.inputs['From Min'].default_value = .02
    outline.inputs['From Max'].default_value = .1
    simple = nodes.new('ShaderNodeMix')
    simple.data_type = 'FLOAT'
    links.new(plain, simple.inputs['Factor'])
    links.new(foam, simple.inputs['A'])
    links.new(outline.outputs['Result'], simple.inputs['B'])
    foam = simple.outputs['Result']
    # Only upward faces: waterfalls and the sides of water keep their look.
    normal = nodes.new('ShaderNodeSeparateXYZ')
    links.new(geometry.outputs['True Normal'], normal.inputs[0])
    top = _math(nodes, links, 'GREATER_THAN', normal.outputs['Z'], .5)
    foam = _math(nodes, links, 'MULTIPLY', _math(nodes, links, 'MULTIPLY', foam, top), amount)
    links.new(_math(nodes, links, 'MINIMUM', foam, 1), group_out.inputs['Foam'])
    tree[MARKER] = VERSION


def group(create=True):
    tree = bpy.data.node_groups.get(GROUP)
    if tree is None and create:
        tree = bpy.data.node_groups.new(GROUP, 'ShaderNodeTree')
    if tree is not None and tree.get(MARKER) != VERSION:
        _build(tree)
    return tree


def _solid_faces(collection, heights, bounds, block, cells=None, shift=(0., 0.)):
    """World-space triangles of the import's solid faces near the water lines.

    With cells (the blocks next to water, in the grid offset by shift), only
    faces whose footprint touches one of them are kept: a world mostly at sea
    level otherwise sends every grass top to the shore bake, which runs into
    MAX_SHORE_TRIANGLES and stops before it reaches the lakes.

    Water and the world's cross-plane plants (diagonal in plan) are left out:
    the shore band hugs blocks, not seagrass.
    """
    from .water import is_water_material
    # Only faces near some water line: one band from the lowest water to the
    # highest took a whole world with underground water (aquifers at -53,
    # a lake at 63), hit MAX_SHORE_TRIANGLES and stopped before the lake.
    # Heights that differ only by float noise are one water line.
    levels = np.unique(np.round(np.asarray(sorted(heights), np.float64) / block * 1024.) / 1024. * block)
    below, above = .05 * block, BANK * block
    table = None
    if cells:
        keys = np.array(list(cells), np.int64).reshape(-1, 2)
        origin = keys.min(axis=0)
        span = keys.max(axis=0) - origin + 1
        grid = np.zeros((span[0] + 1, span[1] + 1), np.int64)
        grid[keys[:, 0] - origin[0] + 1, keys[:, 1] - origin[1] + 1] = 1
        table = grid.cumsum(axis=0).cumsum(axis=1)       # summed-area table, one row/column of zeros in front
        level = np.full((span[0], span[1]), np.nan)     # each block's own water line
        level[keys[:, 0] - origin[0], keys[:, 1] - origin[1]] = [cells[tuple(key)] for key in keys.tolist()]

    def touches(lo, hi):
        """Per triangle: is it at the water line of a block it covers? Faces of
        one or two blocks are checked against those blocks' own water line, so
        caves under a lake are not taken for its shore; large merged faces
        only need to cover a block next to water."""
        if table is None:
            return np.ones(len(lo), bool)
        eps = 1e-3
        x0 = np.floor((lo[:, 0] - eps) / block - shift[0]).astype(np.int64) - origin[0]
        x1 = np.floor((hi[:, 0] + eps) / block - shift[0]).astype(np.int64) - origin[0]
        y0 = np.floor((lo[:, 1] - eps) / block - shift[1]).astype(np.int64) - origin[1]
        y1 = np.floor((hi[:, 1] + eps) / block - shift[1]).astype(np.int64) - origin[1]
        inside = (x1 >= 0) & (y1 >= 0) & (x0 < span[0]) & (y0 < span[1])
        x0, y0 = np.clip(x0, 0, span[0] - 1), np.clip(y0, 0, span[1] - 1)
        x1, y1 = np.clip(x1, 0, span[0] - 1), np.clip(y1, 0, span[1] - 1)
        total = table[x1 + 1, y1 + 1] - table[x0, y1 + 1] - table[x1 + 1, y0] + table[x0, y0]
        result = inside & (total > 0)
        small = result & (x1 - x0 <= 1) & (y1 - y0 <= 1)
        if small.any():
            at_line = np.zeros(int(small.sum()), bool)
            low_z, high_z = lo[small, 2] - above, hi[small, 2] + below
            for xs, ys in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                h = level[xs[small], ys[small]]
                at_line |= ~np.isnan(h) & (h >= low_z) & (h <= high_z)
            result[small] = at_line
        return result
    xmin, xmax, ymin, ymax = bounds
    chunks, total = [], 0

    def highest(chunks):
        """Keep MAX_SHORE_TRIANGLES, the highest first: when a world has more
        water-side faces than that (caves and aquifers everywhere), surface
        lakes and rivers keep their shores and deep cave water goes first."""
        merged = np.concatenate(chunks)
        if len(merged) > MAX_SHORE_TRIANGLES:
            top = merged[:, :, 2].max(axis=1)
            merged = merged[np.argpartition(-top, MAX_SHORE_TRIANGLES - 1)[:MAX_SHORE_TRIANGLES]]
        return [merged]
    for obj in collection.all_objects:
        if obj.type != 'MESH' or obj.get(TAG) or obj.data is None or obj.hide_render:
            continue
        mesh = obj.data
        if not len(mesh.polygons):
            continue
        mesh.calc_loop_triangles()
        count = len(mesh.loop_triangles)
        corners = np.empty(count * 3, np.int32)
        mesh.loop_triangles.foreach_get('vertices', corners)
        materials = np.empty(count, np.int32)
        mesh.loop_triangles.foreach_get('material_index', materials)
        coordinates = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertices.foreach_get('co', coordinates)
        matrix = np.array(obj.matrix_world, np.float64)
        points = coordinates.reshape(-1, 3).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]
        tri = points[corners.reshape(-1, 3)]
        if np.linalg.det(matrix[:3, :3]) < 0:
            # A mirrored import turns faces inside out; face them out again.
            tri = tri[:, ::-1]
        lo, hi = tri.min(axis=1), tri.max(axis=1)
        # A triangle is near a water line h when lo.z <= h + above and hi.z >= h - below.
        nearest = np.searchsorted(levels, lo[:, 2] - above)
        found = nearest < len(levels)
        near_water = np.zeros(len(tri), bool)
        near_water[found] = levels[nearest[found]] <= hi[found, 2] + below
        keep = (near_water & (hi[:, 0] >= xmin) & (lo[:, 0] <= xmax) &
                (hi[:, 1] >= ymin) & (lo[:, 1] <= ymax))
        keep[keep] = touches(lo[keep], hi[keep])
        water = [i for i, slot in enumerate(obj.material_slots) if is_water_material(slot.material)]
        plants = _plant_slots(obj)
        if water or plants:
            keep &= ~np.isin(materials, water + plants)
        normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
        keep &= ~((np.abs(normals[:, 0]) > .3) & (np.abs(normals[:, 1]) > .3) & (np.abs(normals[:, 2]) < .3))
        tri = tri[keep].astype(np.float32)
        if not len(tri):
            continue
        chunks.append(tri)
        total += len(tri)
        if total > MAX_SHORE_TRIANGLES * 1.5:
            chunks = highest(chunks)
            total = len(chunks[0])
    if not chunks:
        return [], []
    tri = highest(chunks)[0].astype(np.float64)
    vertices = list(map(tuple, tri.reshape(-1, 3)))
    triangles = [(3 * i, 3 * i + 1, 3 * i + 2) for i in range(len(tri))]
    return vertices, triangles


def shore_boxes(collection, points, sizes, block, lift=.04):
    """Per block, the box the world's solid blocks fill at the water line.

    A box is in the block's own 0-1 coordinates (x0, x1, y0, y1). Short rays
    across each block next to the water find the faces it has at the height
    the brush works at. A block with only its water-facing side exported
    gives a flat box on that side, which is all a distance from the water
    needs. Imports put blocks on their own grid (MiEx centres them on whole
    numbers), so the grid's offset is read from the water itself. Returns
    ({cell: box}, {cell: height}, (x, y) grid offset in blocks).
    """
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    starts = np.cumsum(sizes) - sizes
    low = np.minimum.reduceat(points, starts, axis=0)
    high = np.maximum.reduceat(points, starts, axis=0)
    # Where block edges fall: the circular mean of the water faces' corners.
    turn = np.mod(low[:, :2] / block, 1.) * 2 * math.pi
    shift = tuple(float(np.mod(math.atan2(np.sin(turn[:, axis]).mean(), np.cos(turn[:, axis]).mean()) / (2 * math.pi), 1.))
                  for axis in range(2))
    sx, sy = shift
    water = {}
    # A face counts for the blocks it covers by at least COVER: faces lifted
    # along a sloped normal shift a hair sideways, and a tighter test let a
    # waterfall's step lend its height to the lake block beside it.
    for (x0, y0, z0), (x1, y1, z1) in zip(low / block - (sx, sy, 0), high / block - (sx, sy, 0)):
        z = (z0 + z1) / 2 * block + lift * block
        for cx in range(math.floor(x0 + COVER), max(math.ceil(x1 - COVER), math.floor(x0 + COVER) + 1)):
            for cy in range(math.floor(y0 + COVER), max(math.ceil(y1 - COVER), math.floor(y0 + COVER) + 1)):
                water[cx, cy] = max(water.get((cx, cy), -math.inf), z)
    if not water:
        return {}, water, shift
    near = {}
    for (cx, cy), z in water.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                key = (cx + dx, cy + dy)
                near[key] = max(near.get(key, -math.inf), z)
    xs = [key[0] for key in near]
    ys = [key[1] for key in near]
    bounds = ((min(xs) - 1 + sx) * block, (max(xs) + 2 + sx) * block, (min(ys) - 1 + sy) * block, (max(ys) + 2 + sy) * block)
    vertices, triangles = _solid_faces(collection, set(near.values()), bounds, block, cells=near, shift=shift)
    if not triangles:
        return {}, water, shift
    tree = BVHTree.FromPolygons(vertices, triangles, all_triangles=True)
    edge = 1e-3 * block
    rim = 2e-3
    boxes = {}
    for (cx, cy), z in near.items():
        x0, y0 = (cx + sx) * block, (cy + sy) * block
        # Nothing at all within reach of the block: open water.
        if tree.find_nearest(Vector((x0 + block / 2, y0 + block / 2, z)), .72 * block)[0] is None:
            continue
        hits = []
        for row in BOX_ROWS:
            along = row * block
            for origin, direction in (((x0 - edge, y0 + along, z), (1, 0, 0)), ((x0 + block + edge, y0 + along, z), (-1, 0, 0)),
                                      ((x0 + along, y0 - edge, z), (0, 1, 0)), ((x0 + along, y0 + block + edge, z), (0, -1, 0))):
                hit, normal, *_ = tree.ray_cast(Vector(origin), Vector(direction), block + 2 * edge)
                if hit is None:
                    continue
                u, v = (hit.x - x0) / block, (hit.y - y0) / block
                if not (-rim <= u <= 1 + rim and -rim <= v <= 1 + rim):
                    continue
                # A face on the block's border belongs to the block it faces
                # out of: the sand side facing the water is the sand block's,
                # not the water block's (imports leave water planes running
                # on under the land, so being water says little). Only faces
                # lying in the border count: a side running across it (the
                # shore face hit by the corner rows) is this block's, or the
                # band missed a quarter of every block and shores came out dashed.
                if ((u < rim and normal.x > .5) or (u > 1 - rim and normal.x < -.5) or
                        (v < rim and normal.y > .5) or (v > 1 - rim and normal.y < -.5)):
                    continue
                hits.append((min(max(u, 0.), 1.), min(max(v, 0.), 1.)))
        if not hits:
            # A bank whose top sits just above the water and whose sides
            # under it were left out of the export.
            found = tree.ray_cast(Vector((x0 + block / 2, y0 + block / 2, z + BANK * block)), Vector((0, 0, -1)), BANK * block)
            if found[0] is not None and found[1].z > .95:
                hits = [(0., 0.), (1., 1.)]
        if hits:
            us, vs = zip(*hits)
            boxes[cx, cy] = (min(us), max(us), min(vs), max(vs))
    return boxes, water, shift


def _build_brush(tree):
    """A square brush: white wherever a square of half-width Foam width around
    the point touches anything. Shores and props get one even band with
    straight sides and sharp, square corners.

    Each sheet face carries the solid boxes of the 3x3 blocks around it; the
    band is the square distance (the larger of the two axis gaps) to the
    nearest one, exact in both engines with no rays traced.
    """
    tree.nodes.clear()
    for socket in list(tree.interface.items_tree):
        tree.interface.remove(socket)
    for name, amount in (('Foam width', .3), ('Prop foam width', .14), ('Foam amount', 1.), ('Block', 1.)):
        item = tree.interface.new_socket(name, in_out='INPUT', socket_type='NodeSocketFloat')
        item.default_value = amount
    tree.interface.new_socket('Foam', in_out='OUTPUT', socket_type='NodeSocketFloat')
    nodes, links = tree.nodes, tree.links
    group_in = nodes.new('NodeGroupInput')
    group_out = nodes.new('NodeGroupOutput')
    width, prop_width, amount, block = (group_in.outputs[name] for name in
                                        ('Foam width', 'Prop foam width', 'Foam amount', 'Block'))

    def attribute(name):
        node = nodes.new('ShaderNodeAttribute')
        node.attribute_type = 'GEOMETRY'
        node.attribute_name = name
        return node

    # Where the point sits in its face's block, 0-1 across it.
    coords = nodes.new('ShaderNodeTexCoord')
    offset = nodes.new('ShaderNodeVectorMath')
    offset.operation = 'SUBTRACT'
    links.new(coords.outputs['Object'], offset.inputs[0])
    links.new(attribute(FOAM_CELL).outputs['Vector'], offset.inputs[1])
    local = nodes.new('ShaderNodeSeparateXYZ')
    links.new(offset.outputs[0], local.inputs[0])
    u = _math(nodes, links, 'DIVIDE', local.outputs['X'], block)
    v = _math(nodes, links, 'DIVIDE', local.outputs['Y'], block)
    def distance(x0, x1, y0, y1, dx, dy):
        """Square distance from the point to a box in the block (dx, dy) away."""
        present = _math(nodes, links, 'GREATER_THAN', _math(nodes, links, 'SUBTRACT', x1, x0), -.5)
        across_u = _math(nodes, links, 'SUBTRACT', u, dx)
        along_v = _math(nodes, links, 'SUBTRACT', v, dy)
        across = _math(nodes, links, 'MAXIMUM', _math(nodes, links, 'MAXIMUM', _math(nodes, links, 'SUBTRACT', x0, across_u),
                                                      _math(nodes, links, 'SUBTRACT', across_u, x1)), 0)
        along = _math(nodes, links, 'MAXIMUM', _math(nodes, links, 'MAXIMUM', _math(nodes, links, 'SUBTRACT', y0, along_v),
                                                     _math(nodes, links, 'SUBTRACT', along_v, y1)), 0)
        return _math(nodes, links, 'ADD', _math(nodes, links, 'MAXIMUM', across, along),
                     _math(nodes, links, 'MULTIPLY', _math(nodes, links, 'SUBTRACT', 1, present), 100))

    shore = props = None
    for index, (dx, dy) in enumerate(NEIGHBOURS):
        box = attribute(FOAM_BOX + str(index))
        split = nodes.new('ShaderNodeSeparateColor')
        links.new(box.outputs['Color'], split.inputs[0])
        values = (*split.outputs, box.outputs['Alpha'])
        # Each value holds the shore box and the prop box: shore * PACK + prop.
        high = [_math(nodes, links, 'DIVIDE', _math(nodes, links, 'FLOOR', _math(nodes, links, 'DIVIDE', value, PACK)), PACK - 1)
                for value in values]
        low = [_math(nodes, links, 'DIVIDE', _math(nodes, links, 'FLOOR', _math(nodes, links, 'MODULO', value, PACK)), PACK - 1)
               for value in values]
        reach = distance(*high, dx, dy)
        shore = reach if shore is None else _math(nodes, links, 'MINIMUM', shore, reach)
        reach = distance(*low, dx, dy)
        props = reach if props is None else _math(nodes, links, 'MINIMUM', props, reach)
    touched = _math(nodes, links, 'MAXIMUM',
                    _math(nodes, links, 'LESS_THAN', _math(nodes, links, 'MULTIPLY', shore, block), width),
                    _math(nodes, links, 'LESS_THAN', _math(nodes, links, 'MULTIPLY', props, block), prop_width))
    geometry = nodes.new('ShaderNodeNewGeometry')
    normal = nodes.new('ShaderNodeSeparateXYZ')
    links.new(geometry.outputs['True Normal'], normal.inputs[0])
    top = _math(nodes, links, 'GREATER_THAN', normal.outputs['Z'], .5)
    foam = _math(nodes, links, 'MULTIPLY', _math(nodes, links, 'MULTIPLY', touched, top), amount)
    links.new(_math(nodes, links, 'MINIMUM', foam, 1), group_out.inputs['Foam'])
    tree[MARKER] = VERSION


def brush_group(create=True):
    tree = bpy.data.node_groups.get(BRUSH_GROUP)
    if tree is None and create:
        tree = bpy.data.node_groups.new(BRUSH_GROUP, 'ShaderNodeTree')
    if tree is not None and tree.get(MARKER) != VERSION:
        _build_brush(tree)
    return tree


def pack(shore, props):
    """One FOAM_BOX value from a shore box and a prop box (exact in float32;
    the half keeps the split clear of interpolation's last-bit noise)."""
    return (np.rint(np.asarray(shore) * (PACK - 1)) * PACK + np.rint(np.asarray(props) * (PACK - 1)) + .5).astype(np.float32)


def unpack(values):
    """(shore, prop) boxes from FOAM_BOX values."""
    values = np.asarray(values, np.float64)
    return np.floor(values / PACK) / (PACK - 1), np.floor(np.mod(values, PACK)) / (PACK - 1)


# ---------------------------------------------------------------- props

_sheets = {}      # sheet name -> what update_props needs, read once per build
_busy = [False]


def _sheet_data(sheet):
    mesh = sheet.data
    key = (mesh.as_pointer(), sheet.get('mc_foam_build'), len(mesh.polygons))
    data = _sheets.get(sheet.name)
    if data is not None and data['key'] == key:
        return data
    count = len(mesh.polygons)
    block = float(sheet.get('mc_foam_block', 1.))
    shift = np.array(tuple(sheet.get('mc_foam_shift', (0., 0.))), np.float64)
    origin = np.empty(count * 3, np.float32)
    mesh.attributes[FOAM_CELL].data.foreach_get('vector', origin)
    cells = np.rint(origin.reshape(-1, 3)[:, :2] / block - shift).astype(np.int64)
    centers = np.empty(count * 3, np.float32)
    mesh.polygons.foreach_get('center', centers)
    heights = centers.reshape(-1, 3)[:, 2].astype(np.float64) + BRUSH_LIFT * block
    shore = []
    for index in range(len(NEIGHBOURS)):
        values = np.empty(count * 4, np.float32)
        mesh.attributes[SHORE_BOX + str(index)].data.foreach_get('color', values)
        shore.append(values.reshape(-1, 4))
    low = cells.min(axis=0) - 2
    span = cells.max(axis=0) - low + 3

    def encode(c):
        return (c[:, 0] - low[0]) * span[1] + (c[:, 1] - low[1])

    neighbours = [encode(cells + offset) for offset in NEIGHBOURS]
    # The water line each block is measured at: its own water's, or a
    # neighbour's for the land around it.
    level = np.full(int(span[0] * span[1]), -np.inf)
    for keys in neighbours:
        np.maximum.at(level, keys, heights)
    data = dict(key=key, block=block, shift=shift, shore=shore, neighbours=neighbours, level=level, low=low,
                span=span, levels=np.unique(np.round(heights, 4)), written=None)
    _sheets[sheet.name] = data
    return data


def _crossings(triangles, height):
    """Segments where triangles cross a horizontal plane, as (n, 2, 2) xy pairs."""
    depth = triangles[:, :, 2] - height
    crossing = (depth.min(axis=1) < 0) & (depth.max(axis=1) > 0)
    triangles, depth = triangles[crossing], depth[crossing]
    if not len(triangles):
        return np.empty((0, 2, 2))
    points = []
    for a, b in ((0, 1), (1, 2), (2, 0)):
        da, db = depth[:, a], depth[:, b]
        cut = (da * db) < 0
        t = np.where(cut, da / np.where(cut, da - db, 1), 0)
        point = triangles[:, a, :2] + (triangles[:, b, :2] - triangles[:, a, :2]) * t[:, None]
        points.append((cut, point))
    # Each crossing triangle has two cut edges: the first two in edge order.
    first = np.where(points[0][0], 0, 1)
    second = np.where(points[0][0], np.where(points[1][0], 1, 2), 2)
    stack = np.stack([p for _, p in points], axis=1)
    rows = np.arange(len(triangles))
    return np.stack((stack[rows, first], stack[rows, second]), axis=1)


def prop_boxes(scene, depsgraph, sheet, data):
    """Per block, the box what is not the world fills at the water line.

    Every visible mesh outside the import that reaches the water (characters,
    boats, props, posed and animated) is cut by the plane its whitewater is
    measured at; the cut outline, sampled finely along each segment, gives
    each block's box. Returns sorted block keys and their boxes.
    """
    block, shift, low, span = data['block'], data['shift'], data['low'], data['span']
    collection = sheet.users_collection[0] if sheet.users_collection else None
    world = {obj.as_pointer() for obj in collection.all_objects} if collection is not None else set()
    to_sheet = np.array(sheet.matrix_world.inverted(), np.float64)
    zmin, zmax = data['levels'].min(), data['levels'].max()
    xmin, ymin = (low + shift) * block
    xmax, ymax = (low + span + shift) * block
    keys, us, vs = [], [], []
    step = block / 32
    for obj in scene.objects:
        if (obj.type != 'MESH' or obj.as_pointer() in world or obj.hide_render or obj.get(TAG) or
                any(obj.get(owner) for owner in SKIP_OWNERS)):
            continue
        evaluated = obj.evaluated_get(depsgraph)
        matrix = to_sheet @ np.array(evaluated.matrix_world, np.float64)
        corners = np.array([tuple(c) for c in evaluated.bound_box], np.float64) @ matrix[:3, :3].T + matrix[:3, 3]
        lo, hi = corners.min(axis=0), corners.max(axis=0)
        if hi[2] < zmin or lo[2] > zmax or hi[0] < xmin or lo[0] > xmax or hi[1] < ymin or lo[1] > ymax:
            continue
        mesh = evaluated.to_mesh()
        if mesh is None:
            continue
        try:
            mesh.calc_loop_triangles()
            count = len(mesh.loop_triangles)
            if not count:
                continue
            corners = np.empty(count * 3, np.int32)
            mesh.loop_triangles.foreach_get('vertices', corners)
            coordinates = np.empty(len(mesh.vertices) * 3, np.float32)
            mesh.vertices.foreach_get('co', coordinates)
        finally:
            evaluated.to_mesh_clear()
        points = coordinates.reshape(-1, 3).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]
        triangles = points[corners.reshape(-1, 3)]
        for height in data['levels']:
            segments = _crossings(triangles, height)
            if not len(segments):
                continue
            # Sample each cut segment finely so every block it passes gets its share.
            lengths = np.linalg.norm(segments[:, 1] - segments[:, 0], axis=1)
            counts = np.ceil(lengths / step).astype(np.int64) + 1
            owner = np.repeat(np.arange(len(segments)), counts)
            t = (np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)) / np.repeat(np.maximum(counts - 1, 1), counts)
            samples = segments[owner, 0] + (segments[owner, 1] - segments[owner, 0]) * t[:, None]
            grid = samples / block - shift
            cells = np.floor(grid).astype(np.int64)
            inside = ((cells[:, 0] >= low[0]) & (cells[:, 0] < low[0] + span[0]) &
                      (cells[:, 1] >= low[1]) & (cells[:, 1] < low[1] + span[1]))
            cells, grid = cells[inside], grid[inside]
            key = (cells[:, 0] - low[0]) * span[1] + (cells[:, 1] - low[1])
            # Only blocks whose water line is this plane.
            here = np.abs(data['level'][key] - height) < .01 * block
            keys.append(key[here])
            us.append((grid[here, 0] - cells[here, 0]).clip(0, 1))
            vs.append((grid[here, 1] - cells[here, 1]).clip(0, 1))
    if not keys or not sum(len(k) for k in keys):
        return np.empty(0, np.int64), np.empty((0, 4), np.float32)
    keys, us, vs = np.concatenate(keys), np.concatenate(us), np.concatenate(vs)
    unique, inverse = np.unique(keys, return_inverse=True)
    boxes = np.tile(np.array(EMPTY_BOX, np.float64), (len(unique), 1))
    np.minimum.at(boxes[:, 0], inverse, us)
    np.maximum.at(boxes[:, 1], inverse, us)
    np.minimum.at(boxes[:, 2], inverse, vs)
    np.maximum.at(boxes[:, 3], inverse, vs)
    return unique, boxes.astype(np.float32)


def update_props(scene, depsgraph, sheet):
    """Pack what stands in the water now with the baked shores; True if the sheet changed."""
    data = _sheet_data(sheet)
    keys, boxes = prop_boxes(scene, depsgraph, sheet, data)
    merged = []
    for shore, neighbour in zip(data['shore'], data['neighbours']):
        props = np.tile(np.array(EMPTY_BOX, np.float32), (len(shore), 1))
        if len(keys):
            at = np.searchsorted(keys, neighbour).clip(0, len(keys) - 1)
            hit = keys[at] == neighbour
            props[hit] = boxes[at[hit]]
        merged.append(pack(shore, props))
    if data['written'] is not None and all(np.array_equal(a, b) for a, b in zip(merged, data['written'])):
        return False
    mesh = sheet.data
    for index, foam in enumerate(merged):
        mesh.attributes[FOAM_BOX + str(index)].data.foreach_set('color', foam.ravel())
    mesh.update()
    data['written'] = merged
    return True


_sheet_names = {}  # scene pointer -> (object count, names of its whitewater sheets)


def _scene_sheets(scene):
    """The scene's whitewater sheets.

    Finding them walks every object, and this runs on every edit and frame,
    so the names are kept until objects come or go or a sheet is renamed.
    """
    count = len(bpy.data.objects)
    cached = _sheet_names.get(scene.as_pointer())
    if cached is None or cached[0] != count:
        cached = _sheet_names[scene.as_pointer()] = (
            count, [obj.name for obj in scene.objects if obj.get(TAG) and obj.type == 'MESH'])
    sheets = [bpy.data.objects.get(name) for name in cached[1]]
    if None in sheets:
        _sheet_names.pop(scene.as_pointer(), None)
    return [obj for obj in sheets if obj is not None and obj.get(TAG) and obj.type == 'MESH'
            and obj.data is not None and FOAM_BOX + '0' in obj.data.attributes]


def refresh(scene, depsgraph=None):
    """Bring every square-brush sheet in the scene up to date with its props."""
    if _busy[0]:
        return
    sheets = _scene_sheets(scene)
    if not sheets:
        return
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    _busy[0] = True
    try:
        for sheet in sheets:
            update_props(scene, depsgraph, sheet)
    finally:
        _busy[0] = False


def _moved(update):
    """Something other than a whitewater sheet moved, posed, changed or went away."""
    target = getattr(update.id, 'original', update.id)
    if isinstance(target, bpy.types.Object):
        return not target.get(TAG)
    return isinstance(target, bpy.types.Collection)


@bpy.app.handlers.persistent
def _after_update(scene, depsgraph):
    if _busy[0] or bpy.app.is_job_running('RENDER'):
        return
    if any(_moved(update) for update in depsgraph.updates):
        refresh(scene, depsgraph)


@bpy.app.handlers.persistent
def _after_frame(scene, depsgraph=None):
    refresh(scene, depsgraph)


def _upgrade_water():
    """Once a file has loaded: Plain water saved by an older Sparkle is applied again."""
    scene = getattr(bpy.context, 'scene', None)
    if scene is not None:
        from . import water
        try:
            water.upgrade(scene)
        except Exception as exc:
            print('Sparkle: Plain water upgrade skipped:', exc)
    return None


@bpy.app.handlers.persistent
def _after_load(*args):
    _sheets.clear()
    _sheet_names.clear()
    scene = getattr(bpy.context, 'scene', None)
    if scene is not None:
        refresh(scene)
        from . import water
        if any(water.outdated(c) for c in scene.collection.children_recursive):
            bpy.app.timers.register(_upgrade_water, first_interval=.2)


HANDLERS = (('depsgraph_update_post', _after_update), ('frame_change_post', _after_frame), ('load_post', _after_load))


def register():
    for name, handler in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler not in handlers:
            handlers.append(handler)


def unregister():
    for name, handler in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler in handlers:
            handlers.remove(handler)
    _sheets.clear()
    _sheet_names.clear()


def _material(scene, preset, block=1.):
    material = bpy.data.materials.new('Sparkle · Contact whitewater')
    material[TAG] = VERSION
    material['mc_edge_exclude'] = material['mc_outline_exclude'] = True
    material.use_nodes = True
    material.diffuse_color = COLOR
    material.use_backface_culling = True
    style = STYLES.get(preset, STYLES['DUNGEONS'])
    if hasattr(material, 'surface_render_method'):
        # Traced foam needs Blended: EEVEE decides a dithered surface's coverage
        # in a pass that cannot trace rays. The square band (Simple, Plain)
        # traces nothing and is solid or clear, so it renders Dithered: a
        # blended sheet over a whole lake asks EEVEE for more shadow pages
        # than it has ("Shadow buffer full", missing shadows).
        material.surface_render_method = 'DITHERED' if style[4] else 'BLENDED'
    elif hasattr(material, 'blend_method'):
        material.blend_method = 'BLEND'
    if hasattr(material, 'use_transparent_shadow'):
        material.use_transparent_shadow = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    contact = nodes.new('ShaderNodeGroup')
    contact.name = contact.label = 'Contact whitewater'
    if style[4]:
        # Simple and Plain: a clean square-brush band.
        contact.node_tree = brush_group()
        contact.inputs['Foam width'].default_value = style[0] * block
        contact.inputs['Prop foam width'].default_value = PROP_WIDTH.get(preset, style[0]) * block
        contact.inputs['Foam amount'].default_value = style[1]
        contact.inputs['Block'].default_value = block
    else:
        contact.node_tree = group()
        for (name, _), amount in zip((DEFAULTS[0], DEFAULTS[1], DEFAULTS[3], DEFAULTS[4], DEFAULTS[5]), style):
            contact.inputs[name].default_value = amount
    material['mcaddon_water_contact_style'] = preset
    clear = nodes.new('ShaderNodeBsdfTransparent')
    if style[4]:
        # Plain white, lit like the shore it rims, so it dims with the night
        # and shadows instead of glowing; foam reflects more than a white
        # block (FOAM_ALBEDO), or the view transform greys it to concrete.
        lit = nodes.new('ShaderNodeBsdfDiffuse')
        lit.inputs['Color'].default_value = (1., 1., 1., 1.)
        extra = nodes.new('ShaderNodeBsdfDiffuse')
        extra.inputs['Color'].default_value = (FOAM_ALBEDO - 1,) * 3 + (1.,)
        whitewater = nodes.new('ShaderNodeAddShader')
        links.new(lit.outputs[0], whitewater.inputs[0])
        links.new(extra.outputs[0], whitewater.inputs[1])
        lit.location, extra.location = (-250, -100), (-250, -250)
    else:
        whitewater = nodes.new('ShaderNodeBsdfDiffuse')
        whitewater.inputs['Color'].default_value = COLOR
    whitewater.name = whitewater.label = 'Whitewater surface'
    mix = nodes.new('ShaderNodeMixShader')
    output = nodes.new('ShaderNodeOutputMaterial')
    if style[4]:
        # The square brush measures blocks and props, never the world's
        # plants, so the shore band still reaches them.
        links.new(contact.outputs['Foam'], mix.inputs[0])
    else:
        # The world's own seagrass and kelp pierce the surface as diagonal
        # cross planes; foam belongs around what is placed in the water.
        allowed = nodes.new('ShaderNodeAttribute')
        allowed.attribute_type = 'GEOMETRY'
        allowed.attribute_name = ALLOWED
        gate = nodes.new('ShaderNodeMath')
        gate.operation = 'MULTIPLY'
        links.new(contact.outputs['Foam'], gate.inputs[0])
        links.new(allowed.outputs['Fac'], gate.inputs[1])
        links.new(gate.outputs[0], mix.inputs[0])
    links.new(clear.outputs[0], mix.inputs[1])
    links.new(whitewater.outputs[0], mix.inputs[2])
    links.new(mix.outputs[0], output.inputs['Surface'])
    for item, location in ((contact, (0, 200)), (clear, (0, 0)), (whitewater, (0, -150)),
                           (mix, (250, 0)), (output, (500, 0))):
        item.location = location
    from .water import prepare_atmosphere
    prepare_atmosphere(scene, material)
    return material


def _cell_planes(points, sizes, block, shift, cells):
    """Per block, the plane z = sx * x + sy * y + c of the highest level water
    top over it (the face shore_boxes measured it by). A sheet quad lying in
    it sits on the water a hair above, sloped or not: flowing water beside a
    lake slopes by about a tenth of a block, and a flat quad at its middle
    height floated over the lake's edge and put a step in the white line."""
    starts = np.cumsum(sizes) - sizes
    p0, p1, p2 = points[starts], points[starts + 1], points[starts + 2]
    normal = np.cross(p1 - p0, p2 - p0)
    flat = np.abs(normal[:, 2]) < 1e-9
    nz = np.where(flat, 1., normal[:, 2])
    slope_x = np.where(flat, 0., -normal[:, 0] / nz)
    slope_y = np.where(flat, 0., -normal[:, 1] / nz)
    centre = np.add.reduceat(points, starts, axis=0) / sizes[:, None]
    offset = centre[:, 2] - slope_x * centre[:, 0] - slope_y * centre[:, 1]
    low = np.minimum.reduceat(points, starts, axis=0) / block - (shift[0], shift[1], 0)
    high = np.maximum.reduceat(points, starts, axis=0) / block - (shift[0], shift[1], 0)
    best = {}
    for index, ((x0, y0, z0), (x1, y1, z1)) in enumerate(zip(low, high)):
        middle = (z0 + z1) / 2
        for cx in range(math.floor(x0 + COVER), max(math.ceil(x1 - COVER), math.floor(x0 + COVER) + 1)):
            for cy in range(math.floor(y0 + COVER), max(math.ceil(y1 - COVER), math.floor(y0 + COVER) + 1)):
                if middle > best.get((cx, cy), (-math.inf, 0))[0]:
                    best[cx, cy] = (middle, index)
    chosen = np.array([best[cell][1] for cell in map(tuple, cells.tolist())], np.int64)
    return slope_x[chosen], slope_y[chosen], offset[chosen]


def plant_cells(collection, water_objects, block=1.):
    """Block cells where the import's own cross-plane plants pierce the water.

    MiEx draws seagrass, kelp and other plants as diagonal cross planes, so a
    face whose normal is diagonal in plan and that spans a water level marks
    its block and the blocks around it.
    """
    skip = {obj.as_pointer() for obj in water_objects}
    cells = set()
    for obj in collection.all_objects:
        if obj.type != 'MESH' or obj.as_pointer() in skip or obj.get(TAG) or obj.data is None:
            continue
        mesh = obj.data
        count = len(mesh.polygons)
        if not count:
            continue
        normals = np.empty(count * 3, np.float32)
        mesh.polygons.foreach_get('normal', normals)
        matrix = np.array(obj.matrix_world, np.float64)
        normals = normals.reshape(-1, 3) @ np.linalg.inv(matrix[:3, :3])
        normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-9)
        diagonal = (np.abs(normals[:, 0]) > .3) & (np.abs(normals[:, 1]) > .3) & (np.abs(normals[:, 2]) < .3)
        plants = _plant_slots(obj)
        if plants:
            # Water plants drawn as upright # planes count too.
            materials = np.empty(count, np.int32)
            mesh.polygons.foreach_get('material_index', materials)
            diagonal |= np.isin(materials, plants)
        if not diagonal.any():
            continue
        centers = np.empty(count * 3, np.float32)
        mesh.polygons.foreach_get('center', centers)
        centers = centers.reshape(-1, 3)[diagonal] @ matrix[:3, :3].T + matrix[:3, 3]
        for x, y in np.unique(np.floor(centers[:, :2] / block).astype(np.int64), axis=0):
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    cells.add((int(x) + dx, int(y) + dy))
    return cells


def owned(collection):
    return [obj for obj in collection.all_objects if obj.get(TAG)]


def remove(collection):
    """Remove this import's whitewater sheet and its unused material."""
    from .water_body import _shared
    if _shared(collection):
        return 0
    allowed = {collection, *collection.children_recursive}
    removed, materials = 0, set()
    for obj in owned(collection):
        if any(c not in allowed for c in obj.users_collection):
            for c in list(obj.users_collection):
                if c in allowed:
                    c.objects.unlink(obj)
            continue
        mesh = obj.data
        materials.update(m for m in mesh.materials if m)
        bpy.data.objects.remove(obj, do_unlink=True)
        if not mesh.users and mesh.get(TAG):
            bpy.data.meshes.remove(mesh)
        removed += 1
    for material in materials:
        if not material.users and material.get(TAG):
            bpy.data.materials.remove(material)
    return removed


def rebuild(scene, collection, preset='DUNGEONS', *, water_objects):
    """Lay a whitewater sheet over the styled water tops; report the face count.

    The import's whitewater style (STYLE) can swap the water's own foam for
    Simple whitewater, a plain white outline.
    """
    if collection.get(STYLE) == 'SIMPLE' and preset != 'PLAIN':
        preset = 'SIMPLE'
    from .water_body import _faces
    from .water_foam import _root
    remove(collection)
    if not supported():
        return dict(faces=0, supported=False)
    block = max(.001, float(collection.get('mcaddon_miex_block_units', 1)))
    points, sizes, participating, truncated = _faces(water_objects, block, inset=-LIFT, up_only=True)
    if sizes is None or not len(sizes):
        return dict(faces=0, supported=True)
    square = bool(STYLES.get(preset, STYLES['DUNGEONS'])[4])
    if square:
        # Level water only: a waterfall's flowing steps slope by most of a
        # block, and a flat band laid over one showed as a white speck
        # floating up the falls.
        starts = np.cumsum(sizes) - sizes
        rise = np.maximum.reduceat(points[:, 2], starts) - np.minimum.reduceat(points[:, 2], starts)
        level = rise <= LEVEL_RISE * block
        if not level.all():
            points, sizes = points[np.repeat(level, sizes)], sizes[level]
        if not len(sizes):
            return dict(faces=0, supported=True)
    if square:
        # The square brush's world shores are measured once, exactly. Its
        # sheet is one quad per water block, each keeping the boxes of the
        # 3x3 blocks around it, whatever size the water faces were.
        boxes, water, shift = shore_boxes(collection, points, sizes, block, lift=BRUSH_LIFT)
        cells = np.array(sorted(water), np.int64).reshape(-1, 2)
        corner = (cells + shift) * block
        square_xy = np.array(((0, 0), (1, 0), (1, 1), (0, 1)), np.float64) * block
        xy = corner[:, None, :] + square_xy[None]
        slope_x, slope_y, offset = _cell_planes(points, sizes, block, shift, cells)
        z = slope_x[:, None] * xy[..., 0] + slope_y[:, None] * xy[..., 1] + offset[:, None]
        points = np.concatenate((xy, z[..., None]), axis=2).reshape(-1, 3)
        sizes = np.full(len(cells), 4, np.int64)
    mesh = bpy.data.meshes.new('Sparkle contact whitewater')
    mesh[TAG] = VERSION
    mesh.vertices.add(len(points))
    mesh.vertices.foreach_set('co', points.astype(np.float32).ravel())
    mesh.loops.add(len(points))
    mesh.loops.foreach_set('vertex_index', np.arange(len(points), dtype=np.int32))
    mesh.polygons.add(len(sizes))
    mesh.polygons.foreach_set('loop_start', (np.cumsum(sizes) - sizes).astype(np.int32))
    mesh.update()
    # Foam may form everywhere except at the world's own plants.
    blocked = plant_cells(collection, water_objects, block)
    allowed = np.ones(len(sizes), np.float32)
    if blocked:
        centers = np.empty(len(sizes) * 3, np.float32)
        mesh.polygons.foreach_get('center', centers)
        keys = np.floor(centers.reshape(-1, 3)[:, :2] / block).astype(np.int64)
        allowed = np.array([0. if (int(x), int(y)) in blocked else 1. for x, y in keys], np.float32)
    mesh.attributes.new(ALLOWED, 'FLOAT', 'FACE').data.foreach_set('value', allowed)
    if square:
        origin = np.zeros((len(sizes), 3), np.float32)
        origin[:, :2] = corner
        mesh.attributes.new(FOAM_CELL, 'FLOAT_VECTOR', 'FACE').data.foreach_set('vector', origin.ravel())
        for index, (dx, dy) in enumerate(NEIGHBOURS):
            values = np.array([boxes.get((int(x) + dx, int(y) + dy), EMPTY_BOX) for x, y in cells], np.float32)
            mesh.attributes.new(SHORE_BOX + str(index), 'FLOAT_COLOR', 'FACE').data.foreach_set('color', values.ravel())
            packed = pack(values, np.tile(np.array(EMPTY_BOX, np.float32), (len(values), 1)))
            mesh.attributes.new(FOAM_BOX + str(index), 'FLOAT_COLOR', 'FACE').data.foreach_set('color', packed.ravel())
    mesh.materials.append(_material(scene, preset, block))
    obj = bpy.data.objects.new('Sparkle · Contact whitewater', mesh)
    obj[TAG], obj['mc_sky_owner'] = VERSION, 'water_contact'
    obj.hide_select = True
    # Foam is seen, not lit through: it casts no shadow and stays out of
    # bounce light, so the sheet costs the water little.
    for name in ('visible_shadow', 'visible_diffuse', 'visible_transmission', 'visible_volume_scatter'):
        if hasattr(obj, name):
            setattr(obj, name, False)
    collection.objects.link(obj)
    root = _root(participating)
    if root and root not in participating:
        obj.parent = root
        obj.matrix_world = Matrix.Identity(4)
    from .water import waves
    if waves(collection, preset):
        # The foam rises and falls with the water's waves.
        from .water_foam import add_bob
        add_bob(obj, scene, block)
    if square:
        obj['mc_foam_block'] = block
        obj['mc_foam_shift'] = shift
        obj['mc_foam_build'] = int.from_bytes(__import__('os').urandom(4), 'little') >> 1
        # Whatever already stands in the water gets its foam now.
        update_props(scene, bpy.context.evaluated_depsgraph_get(), obj)
    return dict(faces=len(sizes), supported=True, truncated=truncated)
