"""Edge data for Sparkle outlines, computed once from the geometry.

Every quad edge is classified against the faces that touch it in world space:

* NONE: the surface continues across it (two blocks side by side), or it is
  an inside corner. No line.
* CONVEX: an outside corner, where two faces fold away from each other. Its
  line becomes a silhouette or an inner edge depending on the view.
* OPEN: nothing continues past it (a sheet, the edge of an export). Always a
  silhouette.

Each corner then stores its distance to every outline edge of its face. The
distances are affine across a quad, so the GPU's interpolation hands every
pixel its exact distance to each edge; the shader needs no rays and no screen
pass. Blender-free (NumPy only), so the classification is tested with pytest.

Bendy player rigs cut every box face into strips so elbows and knees can
bend. With merge, a strip measures from the edges where its whole face ends,
reached by crossing the strips beside it, and a box's thickness spans every
strip of the face folded behind it, so strips draw what one quad would.
"""
import numpy as np

NONE, CONVEX, OPEN = 0, 1, 2
TOLERANCE = 1e-4      # world units; endpoints closer than this are one point
_PRIMES = (0x9E3779B97F4A7C15, 0xC2B2AE3D27D4EB4F, 0x165667B19E3779F9,
           0xD6E8FEB86659FD93, 0xFF51AFD7ED558CCD, 0xC4CEB9FE1A85EC53)


def _unit(vectors):
    length = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.maximum(length, 1e-12)


def frames(corners):
    """Face normals (Q, 3) and in-plane outward edge directions (Q, 4, 3)."""
    normal = _unit(np.cross(corners[:, 2] - corners[:, 0], corners[:, 3] - corners[:, 1]))
    along = _unit(np.roll(corners, -1, axis=1) - corners)
    outward = _unit(np.cross(along, normal[:, None, :]))
    return normal.astype(np.float32), outward.astype(np.float32)


def _hash(columns):
    h = np.zeros(len(columns[0]), np.uint64)
    with np.errstate(over='ignore'):
        for column, prime in zip(columns, _PRIMES):
            h ^= np.asarray(column).astype(np.uint64) * np.uint64(prime)
            h = (h << np.uint64(13)) | (h >> np.uint64(51))
    return h


def _edge_keys(corners, tolerance):
    """(E, 6) int32 endpoint keys, the lower endpoint first, so both windings match."""
    origin = corners.reshape(-1, 3).min(axis=0)
    grid = np.round((corners - origin) / tolerance)
    if grid.max(initial=0) >= 2 ** 31 - 1:
        raise ValueError('This model is too large for edge data at this precision')
    a = grid.astype(np.int32).reshape(-1, 3)
    b = np.roll(grid, -1, axis=1).astype(np.int32).reshape(-1, 3)
    first = ((a[:, 0] < b[:, 0]) | ((a[:, 0] == b[:, 0]) & ((a[:, 1] < b[:, 1]) |
             ((a[:, 1] == b[:, 1]) & (a[:, 2] <= b[:, 2])))))
    return np.concatenate([np.where(first[:, None], a, b), np.where(first[:, None], b, a)], axis=1)


def _matched(normal, outward, keys, side_length):
    """Per edge flags from the other edges at exactly the same place.

    'depth' is how far the folded neighbour reaches away from the edge: on a
    box, the thickness behind this face.
    """
    count = len(keys)
    h = _hash(keys.T)
    order = np.argsort(h)
    hs = h[order]
    del h
    flags = {name: np.zeros(count, bool) for name in ('continues', 'convex', 'sheet', 'neighbours')}
    flags['depth'] = np.zeros(count, np.float32)
    flags['partner'] = np.full(count, -1, np.int64)
    flags['ahead'] = np.full(count, -1, np.int64)
    outward = outward.reshape(-1, 3)
    for offset in range(1, 64):
        i = np.nonzero(hs[:-offset] == hs[offset:])[0]
        if not len(i):
            break
        j = i + offset
        i, j = order[i], order[j]
        exact = np.all(keys[i] == keys[j], axis=1)
        i, j = i[exact], j[exact]
        for x, y in ((i, j), (j, i)):
            nx, ny = normal[x // 4], normal[y // 4]
            ox, oy = outward[x], outward[y]
            dn = np.einsum('ij,ij->i', nx, ny)
            do = np.einsum('ij,ij->i', ox, oy)
            side = np.einsum('ij,ij->i', ny, ox)
            twin = (dn > .99) & (do > .99)          # a duplicate face lying on top
            flags['neighbours'][x[~twin]] = True
            continues = (dn > .99) & (do < -.99)
            flags['continues'][x[continues]] = True
            flags['ahead'][x[continues]] = y[continues]
            flags['sheet'][x[(dn < -.99) & (do > .99)]] = True
            fold = (side > .1) & ~twin
            flags['convex'][x[fold]] = True
            flags['partner'][x[fold]] = y[fold]
            # The neighbour's side running away from the shared edge.
            reach = side_length[(y[fold] // 4) * 4 + (y[fold] + 1) % 4]
            np.maximum.at(flags['depth'], x[fold], reach)
    return flags


class _AxisFaces:
    """Axis-aligned faces bucketed by plane and unit cell, built once.

    Cells are offset by half a cell, so block-aligned faces fall in at most
    four cells each, and only faces on planes that will be queried are kept.
    """

    def __init__(self, plane_axis, plane_sign, lo, hi, faces, planes, tolerance, cell=1.):
        self.lo, self.hi, self.tolerance, self.cell = lo, hi, tolerance, cell
        f_axis, f_sign = plane_axis[faces], plane_sign[faces]
        f_plane = _hash([f_axis, f_sign, np.round(lo[faces, f_axis] / tolerance).astype(np.int64)])
        keep = np.isin(f_plane, planes)
        faces, f_axis, f_plane = faces[keep], f_axis[keep], f_plane[keep]
        u, v = (f_axis + 1) % 3, (f_axis + 2) % 3
        u0, u1 = self.cells(lo[faces, u] - tolerance), self.cells(hi[faces, u] + tolerance)
        v0, v1 = self.cells(lo[faces, v] - tolerance), self.cells(hi[faces, v] + tolerance)
        width = u1 - u0 + 1
        span = width * (v1 - v0 + 1)
        small = np.nonzero(span <= 256)[0]          # huge merged faces fall back to no match
        span = span[small]
        idx = np.repeat(small, span)
        k = np.arange(len(idx)) - np.repeat(np.cumsum(span) - span, span)
        keys = _hash([f_plane[idx], u0[idx] + k % width[idx], v0[idx] + k // width[idx]])
        order = np.argsort(keys)
        self.keys, self.faces = keys[order], faces[idx[order]]

    def cells(self, values):
        return np.floor(values / self.cell - .5).astype(np.int64)

    @staticmethod
    def plane(points, axis, sign, tolerance):
        rows = np.arange(len(points))
        return _hash([axis, sign, np.round(points[rows, axis] / tolerance).astype(np.int64)])

    def find(self, points, axis, sign):
        """For points on axis planes, the index of a face containing each (or -1)."""
        result = np.full(len(points), -1, np.int64)
        if not len(points) or not len(self.keys):
            return result
        rows = np.arange(len(points))
        plane = self.plane(points, axis, sign, self.tolerance)
        keys = _hash([plane, self.cells(points[rows, (axis + 1) % 3]), self.cells(points[rows, (axis + 2) % 3])])
        first = np.searchsorted(self.keys, keys, 'left')
        last = np.searchsorted(self.keys, keys, 'right')
        tolerance = self.tolerance
        for step in range(int((last - first).max(initial=0))):
            live = np.nonzero((first + step < last) & (result < 0))[0]
            if not len(live):
                break
            face = self.faces[first[live] + step]
            inside = ((points[live] >= self.lo[face] - tolerance) & (points[live] <= self.hi[face] + tolerance)).all(axis=1)
            result[live[inside]] = face[inside]
        return result


def _t_junctions(corners, normal, outward, kinds, tolerance):
    """Resolve OPEN axis-aligned edges that end on another face (slabs, paths, stairs)."""
    flat = kinds.reshape(-1)
    candidates = np.nonzero(flat == OPEN)[0]
    if not len(candidates):
        return
    plane_axis = np.argmax(np.abs(normal), axis=1)
    aligned = np.abs(normal[np.arange(len(normal)), plane_axis]) > .999
    plane_sign = np.sign(normal[np.arange(len(normal)), plane_axis]).astype(np.int64)
    face = candidates // 4
    keep = aligned[face]
    candidates, face, edge = candidates[keep], face[keep], candidates[keep] % 4
    if not len(candidates):
        return
    a = corners[face, edge]
    b = corners[face, (edge + 1) % 4]
    mid = (a + b) / 2
    out = outward[face, edge].astype(np.float64)
    step = np.minimum(np.linalg.norm(b - a, axis=1) * .25, .01) + tolerance * 4
    ahead = mid + out * step[:, None]
    out_axis = np.argmax(np.abs(out), axis=1)
    out_aligned = np.abs(out[np.arange(len(out)), out_axis]) > .999
    out_sign = np.sign(out[np.arange(len(out)), out_axis]).astype(np.int64)
    # Every plane any query lands on, so the index is built once.
    planes = [_AxisFaces.plane(ahead, plane_axis[face], plane_sign[face], tolerance)]
    across = np.nonzero(out_aligned)[0]
    for facing in (-1, 1):
        planes.append(_AxisFaces.plane(mid[across], out_axis[across], out_sign[across] * facing, tolerance))
    index = _AxisFaces(plane_axis, plane_sign, corners.min(axis=1), corners.max(axis=1), np.nonzero(aligned)[0],
                       np.unique(np.concatenate(planes)), tolerance)
    # The surface continues on a coplanar face just past the edge.
    hit = index.find(ahead, plane_axis[face], plane_sign[face])
    flat[candidates[hit >= 0]] = NONE
    unresolved = hit < 0
    # The edge lies across a face folding toward it (an inside corner) or away.
    for facing in (-1, 1):
        rows = np.nonzero(unresolved & out_aligned)[0]
        if not len(rows):
            break
        hit = index.find(mid[rows], out_axis[rows], out_sign[rows] * facing)
        found = rows[hit >= 0]
        flat[candidates[found]] = NONE if facing < 0 else CONVEX
        unresolved[found] = False


def _ends(ahead, outward, merged, limit=4096):
    """For every merged edge, the edge where its surface ends straight ahead;
    every other edge keeps itself.

    A continuing edge leads onto the coplanar neighbour across it; the walk
    goes on from that neighbour's opposite edge until an edge folds, opens
    or meets another face. A walk that turns (strips that are not a grid) or
    goes round a closed band keeps the edge itself. World blocks are never
    merged, so a world's fields of block tops are not walked at all.
    """
    end = np.arange(len(ahead))
    start = np.nonzero(merged & (ahead >= 0))[0]
    live = start
    for _ in range(limit):
        if not len(live):
            break
        step = ahead[end[live]]
        end[live] = (step // 4) * 4 + (step % 4 + 2) % 4
        live = live[ahead[end[live]] >= 0]
    end[live] = live
    turned = start[np.einsum('ij,ij->i', outward[end[start]], outward[start]) <= .99]
    end[turned] = turned
    return end


def _line_distance(points, starts, stops):
    """Distance from each point to the line through start and stop."""
    return np.linalg.norm(np.cross(points - starts, _unit(stops - starts)), axis=-1)


def classify(corners, tolerance=TOLERANCE):
    """corners: (Q, 4, 3) world-space quad corners. Returns (Q, 4) int8 kinds.

    Edge k runs from corner k to corner k + 1.
    """
    return classify_full(corners, tolerance)[0]


def classify_full(corners, tolerance=TOLERANCE):
    """Kinds (Q, 4), each face's thickness (Q,) and which convex edges fold
    onto a box face (Q, 4).

    On a box, the face folded over a convex edge folds again at its far side.
    Seen edge-on, that face is a thin sliver and the shape's outline is its far
    side: a line measured from there stays on the outline. On terrain the fold
    meets the ground instead (an inside corner), so no outline lies beyond it.
    """
    return classify_regions(corners, False, tolerance)[:3]


def classify_regions(corners, merge=True, tolerance=TOLERANCE):
    """classify_full, measuring faces cut into strips as whole faces.

    merge is True, False or one flag per face. Edge slot k of a merged face
    takes the kind and box flag of the edge where its surface ends that way
    (_ends), and a fold onto a merged face reaches across all of its strips.
    Also returns lines (Q, 4, 2, 3): the ends of the edge each slot measures
    from, for corner_values.
    """
    corners = np.asarray(corners, np.float64)
    if not len(corners):
        return (np.zeros((0, 4), np.int8), np.zeros(0, np.float32), np.zeros((0, 4), bool),
                np.zeros((0, 4, 2, 3)))
    normal, outward = frames(corners)
    side_length = np.linalg.norm(np.roll(corners, -1, axis=1) - corners, axis=2).astype(np.float32).reshape(-1)
    flags = _matched(normal, outward, _edge_keys(corners, tolerance), side_length)
    kinds = np.full(len(corners) * 4, NONE, np.int8)
    kinds[~flags['neighbours'] | flags['sheet']] = OPEN
    kinds[flags['convex']] = CONVEX
    kinds[flags['continues']] = NONE
    kinds = kinds.reshape(-1, 4)
    _t_junctions(corners, normal, outward, kinds, tolerance)
    flat = kinds.reshape(-1)
    count = len(flat)
    merged = np.repeat(np.broadcast_to(np.asarray(merge, bool), (len(corners),)), 4)
    ends = _ends(flags['ahead'], outward.reshape(-1, 3), merged)
    starts = corners.reshape(-1, 3)
    stops = np.roll(corners, -1, axis=1).reshape(-1, 3)
    partner = flags['partner']
    box = np.zeros(count, bool)
    depth = flags['depth'].copy()
    has = np.nonzero((flat == CONVEX) & (partner >= 0))[0]
    opposite = (partner[has] // 4) * 4 + (partner[has] % 4 + 2) % 4
    far_edge = ends[opposite]
    box[has[flat[far_edge] == CONVEX]] = True
    # A fold onto a face cut into strips is as deep as the whole face: from
    # this edge to where that face folds again.
    walked = far_edge != opposite
    middle = (starts[far_edge[walked]] + stops[far_edge[walked]]) / 2
    depth[has[walked]] = _line_distance(middle, starts[has[walked]], stops[has[walked]])
    kinds = flat[ends].reshape(-1, 4)
    box = box[ends].reshape(-1, 4)
    depth = depth[ends].reshape(-1, 4)
    # Boxes have one thickness per face; average the box folds found.
    thickness = np.where(box.any(axis=1), (depth * box).sum(axis=1) / np.maximum(box.sum(axis=1), 1), 0.)
    lines = np.stack([starts[ends], stops[ends]], axis=1).reshape(-1, 4, 2, 3)
    return kinds, thickness.astype(np.float32), box, lines


def corner_values(corners, kinds, box=None, far=1000., lines=None):
    """(Q, 4, 4) float32 values: [face, corner, edge].

    An outline edge stores the corner's distance to it plus one: negative for
    OPEN edges, plus far for CONVEX edges with no box beyond. Other edges store
    0, the same as a mesh with no data at all. Distances are in the space of
    corners (object space for the shader). lines (Q, 4, 2, 3), in the same
    space, gives the edge each slot measures from; by default its own edge.
    """
    corners = np.asarray(corners, np.float64)
    if lines is None:
        lines = np.stack([corners, np.roll(corners, -1, axis=1)], axis=2)
    lines = np.asarray(lines, np.float64)
    along = _unit(lines[:, :, 1] - lines[:, :, 0])
    values = np.zeros((len(corners), 4, 4), np.float32)
    box = np.ones(kinds.shape, bool) if box is None else box
    for edge in range(4):
        relative = corners - lines[:, edge:edge + 1, 0]
        distance = np.linalg.norm(np.cross(relative, along[:, edge:edge + 1, :]), axis=2)
        kind = kinds[:, edge:edge + 1]
        lift = np.where(box[:, edge:edge + 1], 1., 1. + far)
        values[:, :, edge] = np.where(kind == CONVEX, distance + lift, np.where(kind == OPEN, -(distance + 1), 0))
    return values


def summary(kinds):
    flat = np.asarray(kinds).reshape(-1)
    return {'none': int(np.count_nonzero(flat == NONE)), 'convex': int(np.count_nonzero(flat == CONVEX)),
            'open': int(np.count_nonzero(flat == OPEN))}
