"""Source-palette reduction. No bpy; NumPy is bundled with Blender.

Pixels are straight-alpha, display-encoded RGB. Perceptual calculations never
become output pixels: output is always gathered from the original RGB table.
"""
import heapq
import math
from collections import Counter

import numpy as np


def oklab(rgb):
    rgb = np.asarray(rgb, dtype=np.float64)
    linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    lms = linear @ np.array([[.4122214708, .2119034982, .0883024619],
                             [.5363325363, .6806995451, .2817188376],
                             [.0514459929, .1073969566, .6299787005]])
    return np.cbrt(lms) @ np.array([[.2104542553, 1.9779984951, .0259040371],
                                   [.7936177850, -2.4285922050, .7827717662],
                                   [-.0040720468, .4505937099, -.8086757660]])


DEFAULT_GROUPS = [
    dict(name='Dark suit / charcoal', hue_min=0, hue_max=360, sat_min=0, sat_max=40,
         value_min=0, value_max=22, separate_muted=False),
    dict(name='Neutral grays', hue_min=0, hue_max=360, sat_min=0, sat_max=.5,
         value_min=0, value_max=100, separate_muted=False),
    *[dict(name=name,hue_min=lo,hue_max=hi,sat_min=0,sat_max=100,value_min=0,value_max=100,separate_muted=True)
      for name,lo,hi in [('Red',345,15),('Orange / brown',15,40),('Yellow',40,65),
                         ('Yellow-green / olive',65,90),('Green',90,165),('Cyan',165,195),
                         ('Blue',195,255),('Violet',255,285),('Pink / magenta',285,345)]]
]


def hsv_values(rgb):
    """Hue in degrees, saturation/value in percent; never used as output RGB."""
    rgb = np.asarray(rgb,dtype=np.float64)
    high,low = rgb.max(axis=-1),rgb.min(axis=-1)
    delta = high-low
    saturation = np.divide(delta,high,out=np.zeros_like(delta),where=high>0)
    divisor = np.where(delta>0,delta,1)
    red,green,blue = np.moveaxis(rgb,-1,0)
    dominant = rgb.argmax(axis=-1)
    hue = np.where(dominant==0,((green-blue)/divisor)%6,
                   np.where(dominant==1,(blue-red)/divisor+2,(red-green)/divisor+4))*60
    return hue,saturation*100,high*100


def assign_groups(rgb, groups):
    """First matching range owns a color. Unmatched colors stay unchanged."""
    hue,saturation,value = hsv_values(rgb)
    owner = np.full(hue.shape,-1,dtype=np.int32)
    families = np.full(hue.shape,-1,dtype=np.int32)
    for index,group in enumerate(groups):
        if not group.get('enabled',True):
            continue
        lo,hi = group['hue_min'],group['hue_max']
        if not (0<=lo<=360 and 0<=hi<=360 and
                0<=group['sat_min']<=group['sat_max']<=100 and
                0<=group['value_min']<=group['value_max']<=100):
            raise ValueError('Invalid color range: '+group.get('name',str(index)))
        hmatch = (hue>=lo)&(hue<hi) if lo<hi else (hue>=lo)|(hue<hi) if lo>hi else hue==(lo%360)
        match = ((owner<0)&hmatch&(saturation>=group['sat_min'])&(saturation<=group['sat_max'])&
                 (value>=group['value_min'])&(value<=group['value_max']))
        owner[match] = index
        families[match] = index*2
        if group.get('separate_muted',False):
            families[match & (saturation<=12)] += 1
    return families,owner


def color_families(rgb):
    return assign_groups(rgb,DEFAULT_GROUPS)[0]


class PaletteHierarchy:
    """Cached weighted median partitions; stable tie breaking at every split."""
    def __init__(self, pixels, groups=None, lock_groups=True):
        pixels = np.asarray(pixels, dtype=np.float32)
        if pixels.ndim != 3 or pixels.shape[2] != 4 or not np.isfinite(pixels).all():
            raise ValueError('Expected a finite H × W × 4 image')
        if pixels.min() < 0 or pixels.max() > 1:
            raise ValueError('Only standard-range color images are supported')
        self.source = pixels.copy()
        self.visible = pixels[..., 3] > 0
        self.colors, inverse, self.counts = np.unique(
            pixels[..., :3][self.visible], axis=0, return_inverse=True, return_counts=True)
        self.labels = np.full(pixels.shape[:2], -1, dtype=np.int32)
        self.labels[self.visible] = inverse
        self.lab = oklab(self.colors)
        self.range_settings = [dict(group) for group in (DEFAULT_GROUPS if groups is None else groups)]
        self.lock_groups = lock_groups
        if lock_groups:
            self.families,self.owners = assign_groups(self.colors,self.range_settings)
        else:
            self.families = np.zeros(len(self.colors),dtype=np.int32)
            self.owners = np.full(len(self.colors),-1,dtype=np.int32)
        self.preserve = np.zeros(len(self.colors),dtype=bool)
        if lock_groups:
            missing = self.owners<0
            self.families[missing] = np.arange(np.count_nonzero(missing))+len(self.range_settings)*2
            self.preserve[missing] = True
            for index,group in enumerate(self.range_settings):
                if group.get('preserve',False):
                    self.preserve[self.owners==index] = True
        self.groups = {int(family):np.flatnonzero(self.families==family) for family in np.unique(self.families)}
        self.nodes = []
        self.splits = {family:[] for family in self.groups}
        self.heaps = {family:[] for family in self.groups}
        self.roots = {family:self._node(indices) for family,indices in self.groups.items()}

    def _node(self, indices):
        weights = self.counts[indices]
        mean = np.average(self.lab[indices], axis=0, weights=weights)
        errors = (self.lab[indices] - mean) ** 2
        variance = np.average(errors, axis=0, weights=weights)
        representative = int(indices[np.argmin(errors.sum(axis=1))])
        number = len(self.nodes)
        self.nodes.append((indices, representative, int(np.argmax(variance))))
        if len(indices) > 1:
            heapq.heappush(self.heaps[int(self.families[indices[0]])], (-float(variance.sum() * weights.sum()), number))
        return number

    def _map_family(self, family, count, result):
        splits = self.splits[family]
        while len(splits) < count - 1:
            _, parent = heapq.heappop(self.heaps[family])
            indices, _, axis = self.nodes[parent]
            order = indices[np.argsort(self.lab[indices, axis], kind='stable')]
            cumulative = np.cumsum(self.counts[order])
            middle = max(1, min(len(order)-1, int(np.searchsorted(cumulative, cumulative[-1]/2))+1))
            splits.append((parent, self._node(order[:middle]), self._node(order[middle:])))
        leaves = {self.roots[family]}
        for parent, left, right in splits[:count-1]:
            leaves.remove(parent)
            leaves.update((left, right))
        for leaf in leaves:
            indices, representative, _ = self.nodes[leaf]
            result[indices] = representative

    def convert(self, strength=50, cleanup=0, locked=(), protected=None, tileable=False):
        strength = max(0., min(100., float(strength)))
        cleanup = max(0., min(100., float(cleanup)))
        protected = np.zeros(self.visible.shape, bool) if protected is None else np.asarray(protected, bool)
        if protected.shape != self.visible.shape:
            raise ValueError('Protection mask dimensions do not match source')
        output = self.source.copy()
        total = len(self.colors)
        if not total:
            return output, dict(colors=0, requested=0, limited=False, families=0)
        mapping = np.arange(total)
        count = 0
        for family,indices in self.groups.items():
            retained = len(indices) if strength == 0 else max(1,round(math.exp(math.log(len(indices))*(1-strength/100))))
            owner = self.owners[indices[0]]
            group = self.range_settings[owner] if owner>=0 else {}
            if strength and group.get('shades',0)>0:
                retained = min(len(indices),int(group['shades']))
            if self.preserve[indices].any():
                retained = len(indices)
            count += retained
            if strength:
                self._map_family(family,retained,mapping)
        locked_ids = set()
        for color in locked:
            matches = np.flatnonzero(np.all(self.colors == np.asarray(color, np.float32), axis=1))
            if len(matches):
                index = int(matches[0])
                mapping[index] = index
                locked_ids.add(index)
        if strength:
            for indices in self.groups.values():
                self._nearest(mapping,indices)
        labels = self.labels.copy()
        labels[self.visible] = mapping[labels[self.visible]]
        labels[protected] = self.labels[protected]
        fixed = protected | ~self.visible
        fixed[self.visible] |= self.preserve[self.labels[self.visible]]
        if locked_ids:
            fixed |= np.isin(self.labels, list(locked_ids))
        if cleanup:
            labels = self._cleanup(labels, fixed, cleanup, tileable)
        output[..., :3][self.visible] = self.colors[labels[self.visible]]
        output[protected] = self.source[protected]
        used = len(np.unique(labels[self.visible]))
        used_ids = np.unique(labels[self.visible])
        group_stats = [dict(name=group['name'],source=int(np.count_nonzero(self.owners==index)),
                            result=int(np.count_nonzero(self.owners[used_ids]==index)),
                            pixels=int(self.counts[self.owners==index].sum()))
                       for index,group in enumerate(self.range_settings)] if self.lock_groups else []
        return output, dict(colors=used, requested=count, limited=used > count,
                            families=len(self.groups), groups=group_stats,
                            unmatched=int(np.count_nonzero(self.owners<0)) if self.lock_groups else 0)

    def _nearest(self, mapping, source_indices):
        """Assign to nearest retained color, including locks, with bounded memory.

        Median-cut boxes select the palette; their boundaries should not become
        visible color discontinuities. A small spatial tree avoids N × K storage
        or an additional SciPy dependency for large source palettes.
        """
        def tree(indices):
            values = self.lab[indices]
            bounds = (values.min(axis=0),values.max(axis=0))
            if len(indices) <= 16:
                return bounds, np.sort(indices), None
            axis = int(np.argmax(np.ptp(values,axis=0)))
            order = indices[np.argsort(values[:,axis],kind='stable')]
            mid = len(order)//2
            return bounds, None, (tree(order[:mid]),tree(order[mid:]))
        root = tree(np.unique(mapping[source_indices]))
        for start in range(0,len(source_indices),4096):
            source_ids = source_indices[start:start+4096]
            points = self.lab[source_ids]
            best = mapping[source_ids].copy()
            distance = ((points-self.lab[best])**2).sum(axis=1)
            pending = [(root,np.arange(len(points)))]
            while pending:
                node, indices = pending.pop()
                (low,high), leaf, children = node
                delta = np.maximum(np.maximum(low-points[indices],points[indices]-high),0)
                indices = indices[(delta**2).sum(axis=1) <= distance[indices]+1e-15]
                if not len(indices):
                    continue
                if leaf is None:
                    pending.extend((child,indices) for child in children)
                    continue
                errors = ((points[indices,None,:]-self.lab[leaf][None,:,:])**2).sum(axis=2)
                chosen = errors.argmin(axis=1)
                costs = errors[np.arange(len(indices)),chosen]
                colors = leaf[chosen]
                update = (costs < distance[indices]) | ((costs == distance[indices]) & (colors < best[indices]))
                selected = indices[update]
                best[selected],distance[selected] = colors[update],costs[update]
            mapping[source_ids] = best
        return mapping

    def _cleanup(self, labels, fixed, strength, tileable):
        """Only merge complete small components with low-contrast boundaries.

        Read from an immutable label map so traversal order cannot cascade.
        Fixed/alpha boundaries veto a merge, rather than merely excluding votes.
        """
        height, width = labels.shape
        seen = np.zeros(labels.shape, bool)
        result = labels.copy()
        max_area = max(1, round(1 + 15 * strength/100))
        max_distance = .015 + .085 * strength/100
        populations = np.bincount(labels[labels >= 0],minlength=len(self.colors))

        def neighbors(x, y):
            for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
                if tileable:
                    yield nx % width, ny % height
                elif 0 <= nx < width and 0 <= ny < height:
                    yield nx, ny

        for y in range(height):
            for x in range(width):
                if seen[y, x] or fixed[y, x]:
                    continue
                label = labels[y, x]
                stack, component, boundary = [(x, y)], [], Counter()
                seen[y, x] = True
                blocked = False
                while stack:
                    px, py = stack.pop()
                    component.append((px, py))
                    for nx, ny in neighbors(px, py):
                        if fixed[ny, nx] or self.source[ny, nx, 3] != self.source[py, px, 3]:
                            blocked = True
                            continue
                        other = labels[ny, nx]
                        if self.families[other] != self.families[label]:
                            blocked = True
                            continue
                        if other == label:
                            if not seen[ny, nx]:
                                seen[ny, nx] = True
                                stack.append((nx, ny))
                        else:
                            # Compare original colors too: reduction must not conceal an edge.
                            distance = np.linalg.norm(self.lab[self.labels[ny, nx]] - self.lab[self.labels[py, px]])
                            if distance > max_distance:
                                blocked = True
                            boundary[int(other)] += 1
                if blocked or len(component) > max_area or not boundary:
                    continue
                candidates = [key for key in boundary if (populations[key],-key) > (populations[label],-label)]
                if not candidates:
                    continue  # Never swap equally small neighboring patches.
                replacement = min(candidates, key=lambda key: (-boundary[key], key))
                if np.linalg.norm(self.lab[label]-self.lab[replacement]) <= max_distance:
                    for px, py in component:
                        result[py, px] = replacement
        return result
