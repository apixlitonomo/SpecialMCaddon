"""World-map math and MiEx experimental-28 tile decoding, without Blender."""
import base64
from dataclasses import dataclass
import math
from array import array
import sys


@dataclass
class MapView:
    x: float = 0
    z: float = 0
    scale: float = 1.0

    @staticmethod
    def minimum_scale(rect):
        # Pages batch 256 chunks per draw. Keep requests bounded at wide zooms.
        return max(.0625, rect[2] / (16 * 250), rect[3] / (16 * 250))

    def world(self, px, py, rect):
        x, y, w, h = rect
        return self.x + (px - x - w / 2) / self.scale, self.z - (py - y - h / 2) / self.scale

    def screen(self, wx, wz, rect):
        x, y, w, h = rect
        return x + w / 2 + (wx - self.x) * self.scale, y + h / 2 - (wz - self.z) * self.scale

    def zoom(self, factor, px, py, rect):
        before = self.world(px, py, rect)
        self.scale = max(self.minimum_scale(rect), min(48, self.scale * factor))
        after = self.world(px, py, rect)
        self.x += before[0] - after[0]
        self.z += before[1] - after[1]

    def fit(self, bounds, rect):
        x0, z0, x1, z1 = bounds
        self.x, self.z = (x0 + x1 + 1) / 2, (z0 + z1 + 1) / 2
        self.scale = max(self.minimum_scale(rect), min(24, min(rect[2] / max(16, x1 - x0 + 1),
                                         rect[3] / max(16, z1 - z0 + 1)) * .8))

    def visible(self, rect):
        x, z = self.world(rect[0], rect[1] + rect[3], rect)
        xx, zz = self.world(rect[0] + rect[2], rect[1], rect)
        coords = [(cx, cz) for cz in range(math.floor(z / 16), math.floor(zz / 16) + 1)
                  for cx in range(math.floor(x / 16), math.floor(xx / 16) + 1)]
        return sorted(coords, key=lambda p: (p[0] * 16 + 8 - self.x) ** 2 + (p[1] * 16 + 8 - self.z) ** 2)

    def page_keys(self, rect):
        """Drawing needs pages, not a sorted list of every visible chunk."""
        x0,z0=self.world(rect[0],rect[1]+rect[3],rect)
        x1,z1=self.world(rect[0]+rect[2],rect[1],rect)
        return {(x,z) for z in range(math.floor(z0/256),math.floor(z1/256)+1)
                for x in range(math.floor(x0/256),math.floor(x1/256)+1)}


def drag_bounds(start, end, snap=False):
    x0, x1 = sorted((math.floor(start[0]), math.floor(end[0])))
    z0, z1 = sorted((math.floor(start[1]), math.floor(end[1])))
    if snap:
        x0, z0 = x0 // 16 * 16, z0 // 16 * 16
        x1, z1 = x1 // 16 * 16 + 15, z1 // 16 * 16 + 15
    return x0, z0, x1, z1


def decode_chunk(row):
    """Upstream Util.toBase64 actually writes little-endian BGRA and shorts."""
    data = base64.b64decode(row.get('image', ''), validate=True)
    if len(data) != 1024:
        raise ValueError('MiEx chunk image must contain 16 x 16 BGRA pixels')
    rgba = bytes(c for i in range(0, 1024, 4) for c in (data[i+2], data[i+1], data[i], data[i+3]))
    height = base64.b64decode(row.get('heightmap', ''), validate=True)
    heights = array('h') if len(height) == 512 else None
    if heights is not None:
        heights.frombytes(height)
        if sys.byteorder!='little':heights.byteswap()
    return rgba, heights


def exclusions(chunks, min_y, max_y):
    """Absolute block regions avoid MiEx's relative export-chunk indexing."""
    return [dict(minX=x*16, minZ=z*16, maxX=x*16+15, maxZ=z*16+15,
                 minY=min_y, maxY=max_y) for x, z in sorted(chunks)]
