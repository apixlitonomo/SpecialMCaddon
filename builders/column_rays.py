"""The same hits as scene.ray_cast for vertical rays in known block columns, faster.

Water caustics and the water depth map cast vertical rays from every water
column. scene.ray_cast tests every object in the scene on each call: about
0.1 to 0.3 ms a ray on a 700-object MiEx world, so the bakes ran into their
time limits and slower computers got less of the water than faster ones.

Built once per bake, ColumnRays keeps, in world space and one BVH, only the
evaluated triangles a vertical ray inside those columns can reach: about 2 us
a ray. Walls are left out (a vertical ray only grazes a face parallel to it,
and the bakes sample block centres and corners, never block edges), and so is
anything below where the rays stop. Any other ray goes to scene.ray_cast.

Columns follow the world's own block grid: MiEx centres blocks on whole
numbers, so their edges fall at .5 (grid_shift reads that from the water).
"""
import math

import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

# Object types scene.ray_cast hits through their evaluated mesh.
GEOMETRY = {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}
# Faces a column touches only along its edge are never reached from inside it.
EDGE = 1e-6
# Steeper than this a face is a wall: |normal z| of a face Blender rotated upright.
WALL = 1e-6


def grid_shift(corners, cell):
    """Where block edges fall, in cells (0 to 1 on x and y): the circular mean
    of the water faces' lowest corners, as whitewater reads it."""
    corners = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
    if not len(corners):
        return 0., 0.
    turn = np.mod(corners / cell, 1.) * 2 * math.pi
    return tuple(float(np.mod(math.atan2(np.sin(turn[:, axis]).mean(), np.cos(turn[:, axis]).mean()) / (2 * math.pi), 1.))
                 for axis in range(2))


def cells(low, high, cell, shift, cover=1e-4):
    """The block columns a face from low to high covers on one axis."""
    first = math.floor(low / cell - shift + cover)
    return range(first, max(math.ceil(high / cell - shift - cover), first + 1))


class ColumnRays:
    def __init__(self, scene, depsgraph, levels, cell, reach, upward=True, shift=(0., 0.)):
        """``levels`` maps block columns (x, y) of size ``cell``, offset by
        ``shift`` cells, to the lowest water level in them. Rays start in those
        columns and go down at most ``reach`` below that level, and up without
        limit when ``upward``."""
        self.scene, self.cell, self.shift = scene, cell, shift
        self.columns = set(levels)
        self.objects, self.matrices = [], []
        self.triangles = 0
        if not levels:
            self.tree = None
            return
        keys = np.array(list(levels), dtype=np.int64).reshape(-1, 2)
        x0, y0 = keys.min(0)
        size = keys.max(0) - (x0, y0) + 1
        lowest = np.full(tuple(size), np.inf)
        np.minimum.at(lowest, (keys[:, 0] - x0, keys[:, 1] - y0), np.fromiter(levels.values(), float, len(levels)))
        # Columns per rectangle, for faces that span more than one column.
        filled = np.zeros(tuple(size + 1), dtype=np.int64)
        filled[1:, 1:] = np.isfinite(lowest).cumsum(0).cumsum(1)
        floor = float(lowest[np.isfinite(lowest)].min()) - reach
        highest = np.where(np.isfinite(lowest), lowest, -np.inf).max()
        sx, sy = shift

        def covered(lo_x, hi_x, lo_y, hi_y):
            """Water columns in each cell rectangle (inclusive, before clipping)."""
            ax, bx = np.clip(lo_x, 0, size[0]), np.clip(hi_x + 1, 0, size[0])
            ay, by = np.clip(lo_y, 0, size[1]), np.clip(hi_y + 1, 0, size[1])
            return filled[bx, by] - filled[ax, by] - filled[bx, ay] + filled[ax, ay]

        vertices, triangles, owners, polygons = [], [], [], []
        offset = 0
        for instance in depsgraph.object_instances:
            obj = instance.object
            if obj.type not in GEOMETRY:
                continue
            matrix = np.array(instance.matrix_world, dtype=np.float64)
            # Most of a world lies nowhere near its water: skip it unread.
            box = np.array(obj.bound_box, dtype=np.float64) @ matrix[:3, :3].T + matrix[:3, 3]
            low, high = box.min(0), box.max(0)
            if (high[2] < floor or (not upward and low[2] > highest) or
                    not covered(*(np.floor(np.array([low[0] / cell - sx, high[0] / cell - sx,
                                                     low[1] / cell - sy, high[1] / cell - sy])).astype(np.int64)
                                  - (x0, x0, y0, y0)))):
                continue
            temporary = obj.type != 'MESH'
            try:
                mesh = obj.to_mesh() if temporary else obj.data
            except RuntimeError:
                continue
            try:
                count = len(mesh.loop_triangles) if mesh else 0
                if not count:
                    continue
                corners = np.empty(count * 3, np.int32)
                mesh.loop_triangles.foreach_get('vertices', corners)
                faces = np.empty(count, np.int32)
                mesh.loop_triangles.foreach_get('polygon_index', faces)
                local = np.empty(len(mesh.vertices) * 3, np.float32)
                mesh.vertices.foreach_get('co', local)
            finally:
                if temporary:
                    obj.to_mesh_clear()
            world = local.reshape(-1, 3).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]
            tris = world[corners].reshape(count, 3, 3)
            x, y, z = tris[:, :, 0], tris[:, :, 1], tris[:, :, 2]
            normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
            walls = np.abs(normals[:, 2]) <= WALL * np.linalg.norm(normals, axis=1)
            lo_x = np.floor(x.min(1) / cell - sx + EDGE).astype(np.int64) - x0
            hi_x = np.floor(x.max(1) / cell - sx - EDGE).astype(np.int64) - x0
            lo_y = np.floor(y.min(1) / cell - sy + EDGE).astype(np.int64) - y0
            hi_y = np.floor(y.max(1) / cell - sy - EDGE).astype(np.int64) - y0
            top, bottom = z.max(1), z.min(1)
            inside = (hi_x >= 0) & (lo_x < size[0]) & (hi_y >= 0) & (lo_y < size[1]) & ~walls
            if not upward:
                inside &= bottom <= highest
            single = inside & (lo_x == hi_x) & (lo_y == hi_y)
            keep = np.zeros(count, bool)
            if single.any():
                level = lowest[lo_x[single], lo_y[single]]
                keep[single] = top[single] >= level - reach
                if not upward:
                    keep[single] &= bottom[single] <= level
            spread = inside & ~single
            if spread.any():
                keep[spread] = (covered(lo_x[spread], hi_x[spread], lo_y[spread], hi_y[spread]) > 0) & (top[spread] >= floor)
            if not keep.any():
                continue
            kept = corners.reshape(count, 3)[keep]
            if np.linalg.det(matrix[:3, :3]) < 0:
                # scene.ray_cast turns the local normal with the object; a
                # mirroring matrix reverses the winding of world triangles.
                kept = kept[:, (0, 2, 1)]
            used, local_index = np.unique(kept, return_inverse=True)
            vertices.append(world[used])
            triangles.append(local_index.reshape(-1, 3) + offset)
            offset += len(used)
            polygons.append(faces[keep])
            owners.append(np.full(len(kept), len(self.objects), np.int32))
            self.objects.append(obj.original)
            self.matrices.append(Matrix(instance.matrix_world))
        if not triangles:
            self.tree = None
            return
        self.polygons = np.concatenate(polygons)
        self.owners = np.concatenate(owners)
        self.triangles = len(self.polygons)
        self.tree = BVHTree.FromPolygons(np.concatenate(vertices).astype(np.float32), np.concatenate(triangles),
                                         all_triangles=True)

    def ray_cast(self, depsgraph, origin, direction, distance=1.70141e38):
        """scene.ray_cast's result: found, location, normal, polygon index, object, matrix."""
        column = (math.floor(origin.x / self.cell - self.shift[0]), math.floor(origin.y / self.cell - self.shift[1]))
        if direction.x or direction.y or column not in self.columns:
            return self.scene.ray_cast(depsgraph, origin, direction, distance=distance)
        if self.tree is None:
            return False, Vector(), Vector(), -1, None, Matrix()
        location, normal, index, _distance = self.tree.ray_cast(origin, direction, distance)
        if location is None:
            return False, Vector(), Vector(), -1, None, Matrix()
        owner = int(self.owners[index])
        return True, location, normal, int(self.polygons[index]), self.objects[owner], self.matrices[owner]
