"""Palette reduction, speck removal and recolor for Posterize. No bpy; NumPy
is bundled with Blender.

Pixels are straight-alpha, display-encoded RGB (H x W x 4, 0..1). Reduction
and speck removal only ever pick colors the source already has; recolor is
the one step that makes new ones. Alpha and size never change.
"""
import heapq
import itertools

import numpy as np

# Below this OKLab chroma a color is a gray: its hue is noise, so grays form
# one family instead of scattering over the hue families.
NEUTRAL_CHROMA = .03
HUE_FAMILIES = 12
MAX_COLORS = 64


def oklab(rgb):
    rgb = np.asarray(rgb, dtype=np.float64)
    linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    lms = linear @ np.array([[.4122214708, .2119034982, .0883024619],
                             [.5363325363, .6806995451, .2817188376],
                             [.0514459929, .1073969566, .6299787005]])
    return np.cbrt(lms) @ np.array([[.2104542553, 1.9779984951, .0259040371],
                                   [.7936177850, -2.4285922050, .7827717662],
                                   [-.0040720468, .4505937099, -.8086757660]])


def hsv_values(rgb):
    """Hue in degrees, saturation and brightness in percent."""
    rgb = np.asarray(rgb, dtype=np.float64)
    high, low = rgb.max(axis=-1), rgb.min(axis=-1)
    delta = high - low
    saturation = np.divide(delta, high, out=np.zeros_like(delta), where=high > 0)
    divisor = np.where(delta > 0, delta, 1)
    red, green, blue = np.moveaxis(rgb, -1, 0)
    dominant = rgb.argmax(axis=-1)
    hue = np.where(dominant == 0, ((green - blue) / divisor) % 6,
                   np.where(dominant == 1, (blue - red) / divisor + 2, (red - green) / divisor + 4)) * 60
    return hue, saturation * 100, high * 100


def families(lab):
    """0 for grays, else 1 + the color's OKLab hue sector."""
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    sector = np.floor((np.arctan2(lab[:, 2], lab[:, 1]) + np.pi) / (2 * np.pi) * HUE_FAMILIES)
    return np.where(chroma < NEUTRAL_CHROMA, 0, 1 + sector.astype(np.int32) % HUE_FAMILIES)


class Posterizer:
    """One source image. Palettes are cached per color count."""

    def __init__(self, pixels):
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
        self.labels[self.visible] = inverse.ravel()
        self.lab = oklab(self.colors)
        self.families = families(self.lab) if len(self.colors) else np.zeros(0, np.int32)
        self._palettes = {}

    def palette(self, count):
        """Indices of ``count`` representative source colors (median cut in OKLab).

        Every color family starts as its own box when ``count`` allows, so a
        small accent keeps its hue instead of turning into its neighbor.
        """
        total = len(self.colors)
        count = max(1, min(int(count), total))
        if count in self._palettes:
            return self._palettes[count]
        groups = [np.flatnonzero(self.families == family) for family in np.unique(self.families)]
        if count < len(groups):
            groups = [np.arange(total)]
        boxes, order = [], itertools.count()

        def push(indices):
            weights = self.counts[indices]
            mean = np.average(self.lab[indices], axis=0, weights=weights)
            spread = np.average((self.lab[indices] - mean) ** 2, axis=0, weights=weights)
            # Pixels × spread: big, varied areas split first.
            priority = -float(spread.sum() * weights.sum()) if len(indices) > 1 else 0.
            heapq.heappush(boxes, (priority, next(order), indices, int(np.argmax(spread))))

        for indices in groups:
            push(indices)
        while len(boxes) < count and boxes[0][0] < 0:
            _, _, indices, axis = heapq.heappop(boxes)
            ranked = indices[np.argsort(self.lab[indices, axis], kind='stable')]
            cumulative = np.cumsum(self.counts[ranked])
            middle = max(1, min(len(ranked) - 1, int(np.searchsorted(cumulative, cumulative[-1] / 2)) + 1))
            push(ranked[:middle])
            push(ranked[middle:])
        chosen = []
        for _, _, indices, _ in boxes:
            mean = np.average(self.lab[indices], axis=0, weights=self.counts[indices])
            chosen.append(int(indices[np.argmin(((self.lab[indices] - mean) ** 2).sum(axis=1))]))
        self._palettes[count] = np.unique(chosen)
        return self._palettes[count]

    def _nearest(self, palette):
        """Each source color's closest palette color, preferring its own family."""
        mapping = np.empty(len(self.colors), np.int64)
        same_family = len(np.unique(self.families[palette])) == len(np.unique(self.families))
        for start in range(0, len(self.colors), 16384):
            part = slice(start, start + 16384)
            distance = ((self.lab[part, None, :] - self.lab[None, palette, :]) ** 2).sum(axis=2)
            if same_family:
                distance += (self.families[part, None] != self.families[None, palette]) * 1e3
            mapping[part] = palette[distance.argmin(axis=1)]
        return mapping

    def convert(self, colors=0, specks=0, kept=(), protected=None, tileable=False):
        """Reduce to ``colors`` (0 keeps every color), then remove specks.

        ``kept`` source colors stay exactly; ``protected`` pixels keep their
        source color. Returns (pixels, stats).
        """
        protected = np.zeros(self.visible.shape, bool) if protected is None else np.asarray(protected, bool)
        if protected.shape != self.visible.shape:
            raise ValueError('Protection mask dimensions do not match source')
        output = self.source.copy()
        if not len(self.colors):
            return output, dict(colors=0, source_colors=0, palette=[])
        kept_ids = [int(i) for color in kept
                    for i in np.flatnonzero(np.all(self.colors == np.asarray(color, np.float32), axis=1))[:1]]
        mapping = np.arange(len(self.colors))
        if colors:
            palette = np.union1d(self.palette(max(1, int(colors) - len(kept_ids))), kept_ids).astype(np.int64)
            mapping = self._nearest(palette)
            mapping[kept_ids] = kept_ids
        labels = self.labels.copy()
        labels[self.visible] = mapping[labels[self.visible]]
        fixed = protected | ~self.visible
        if kept_ids:
            fixed |= np.isin(self.labels, kept_ids)
        labels[protected] = self.labels[protected]
        if specks:
            labels = self.despeckle(labels, fixed, specks, tileable)
        output[..., :3][self.visible] = self.colors[labels[self.visible]]
        used, area = np.unique(labels[self.visible], return_counts=True)
        order = used[np.argsort(-area, kind='stable')][:MAX_COLORS]
        return output, dict(colors=len(used), source_colors=len(self.colors),
                            palette=[tuple(float(c) for c in self.colors[i]) for i in order])

    def despeckle(self, labels, fixed, strength, tileable=False):
        """Replace lone pixels with the color most of their neighbors share.

        A pixel is a speck when no side neighbor (two or fewer above half
        strength) has its color. Its replacement must be close in OKLab, so
        edges and outlines stay. Transparent pixels don't vote but never
        block, so item edges are cleaned too.
        """
        strength = max(0., min(100., float(strength)))
        alone = 1 if strength > 50 else 0
        reach = .03 + .17 * strength / 100
        passes = 1 + int(strength > 75)
        offsets = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]
        for _ in range(passes):
            if tileable:
                around = np.stack([np.roll(labels, (dy, dx), axis=(0, 1)) for dy, dx in offsets])
            else:
                padded = np.pad(labels, 1, constant_values=-1)
                h, w = labels.shape
                around = np.stack([padded[1 - dy:1 - dy + h, 1 - dx:1 - dx + w] for dy, dx in offsets])
            same_sides = (around[:4] == labels).sum(axis=0)
            votes = np.stack([((around == around[k]) & (around[k] >= 0)).sum(axis=0) for k in range(8)])
            votes[around < 0] = 0
            best = np.take_along_axis(around, votes.argmax(axis=0)[None], 0)[0]
            speck = (~fixed) & (labels >= 0) & (best >= 0) & (best != labels) & (same_sides <= alone)
            speck &= votes.max(axis=0) >= 2
            ys, xs = np.nonzero(speck)
            close = np.linalg.norm(self.lab[labels[ys, xs]] - self.lab[best[ys, xs]], axis=1) <= reach
            if not close.any():
                break
            labels = labels.copy()
            labels[ys[close], xs[close]] = best[ys[close], xs[close]]
        return labels


def recolor_selection(source, options):
    """Visible source pixels a recolor changes: whites and light grays, or
    (PICKED) shades near one picked color."""
    visible = source[..., 3] > 0
    tolerance = max(0., min(100., float(options.get('tolerance', 25))))
    if options.get('select') == 'PICKED':
        pick = oklab(np.asarray(options['pick'][:3], np.float64))
        distance = np.linalg.norm(oklab(source[..., :3]) - pick, axis=-1)
        return visible & (distance <= .001 + .004 * tolerance)
    _, saturation, value = hsv_values(source[..., :3])
    return visible & (saturation <= 10 + .3 * tolerance) & (value >= 70 - .6 * tolerance)


def recolor(output, source, protected, options):
    """Color the selected pixels of ``output``, keeping their shading.

    TINT multiplies like Minecraft's tints (white becomes the color); MATCH
    scales so the brightest selected shade becomes exactly the color.
    Protected pixels are left alone. Returns (pixels, count).
    """
    selected = recolor_selection(source, options) & ~np.asarray(protected, bool)
    count = int(np.count_nonzero(selected))
    if not count:
        return output, 0
    output = output.copy()
    color = np.asarray(options['color'][:3], np.float32)
    shades = output[..., :3][selected]
    if options.get('mode') == 'MATCH':
        brightness = shades @ np.asarray([.2126, .7152, .0722], np.float32)
        shades = brightness[:, None] / max(float(brightness.max()), 1e-6) * color
    else:
        shades = shades * color
    output[..., :3][selected] = np.clip(shades, 0, 1)
    return output, count

