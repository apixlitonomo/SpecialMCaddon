"""UI-side thumbnail lookup: dependency scans belong to the helper process.

Every visible card asks for its thumbnail on every draw, so answers are kept
for a moment instead of touching the disk each time; new worker output drops
them at once (``forget_lookups``).
"""
import hashlib
import os
import json
import struct
import time

from .thumbnail_cache import ThumbnailCache, FAILED_SUFFIX
from .preview_fallback import png_has_visible_pixels, remember_visible_png

# Textures up to this size draw from their own file. Larger ones (HD and RTX
# packs ship 512-4096 px) get a downscaled PNG from the thumbnail worker, so a
# card never loads a full-size texture.
TEXTURE_THUMB_LIMIT = 256
TEXTURE_THUMB_VERSION = 1
# A JPEG or unknown image this large is treated as bigger than the limit.
_UNKNOWN_IMAGE_BYTES = 128 * 1024
# Seconds a lookup answer is reused while no new thumbnails arrive.
LOOKUP_SECONDS = 2.0
# Larger requests (the native "Generate Thumbnails") leave per-file checks to
# the worker and to a helper thread instead of Blender's UI thread.
LARGE_REQUEST = 256

_generation = 0


def forget_lookups():
    """Thumbnails were written or looked up: answer every card from disk again."""
    global _generation
    _generation += 1


def _image_size(path):
    """``(width, height)`` from a PNG or TGA header, else ``None``."""
    try:
        with open(path, 'rb') as stream:
            head = stream.read(24)
    except OSError:
        return None
    if len(head) >= 24 and head[:8] == b'\x89PNG\r\n\x1a\n' and head[12:16] == b'IHDR':
        return struct.unpack('>II', head[16:24])
    if str(path).lower().endswith('.tga') and len(head) >= 18:
        return struct.unpack('<HH', head[12:16])
    return None


class BrowserThumbnailCache(ThumbnailCache):
    def __init__(self, *args, choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.choices = choices or {}
        self.paths = {}
        self.jobs = {}
        self.helper_state = {}
        # The last path lookup found cards still to render: worth starting a
        # worker while the next lookup runs.
        self.needs_renders = False
        self._answers = {}
        self._textures = {}

    @staticmethod
    def asset_key(file_path, asset_type, identifier, geometry_index=-1):
        return (str(file_path), str(asset_type), str(identifier), int(geometry_index))

    def _answer(self, key, compute):
        now = time.monotonic()
        entry = self._answers.get(key)
        if entry is not None and entry[0] == _generation and now < entry[1]:
            return entry[2]
        value = compute()
        if len(self._answers) > 8192:
            self._answers.clear()
        self._answers[key] = (_generation, now + LOOKUP_SECONDS, value)
        return value

    def texture_thumbnail(self, file_path):
        """``None`` for a missing file, ``''`` when the texture draws as it is,
        else the downscaled PNG's path (which may not exist yet)."""
        try:
            stat = os.stat(file_path)
        except (OSError, TypeError, ValueError):
            return None
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self._textures.get(file_path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        size = _image_size(file_path)
        large = max(size) > TEXTURE_THUMB_LIMIT if size else stat.st_size > _UNKNOWN_IMAGE_BYTES
        result = ''
        if large:
            raw = '|'.join((os.path.normcase(os.path.abspath(str(file_path))), str(stat.st_mtime_ns),
                            str(stat.st_size), 'texture', str(TEXTURE_THUMB_VERSION), str(TEXTURE_THUMB_LIMIT)))
            digest = hashlib.md5(raw.encode('utf-8', 'ignore')).hexdigest()
            result = str(self._cache_dir / ('tex_' + digest + '.png'))
        if len(self._textures) > 8192:
            self._textures.clear()
        self._textures[file_path] = (signature, result)
        return result

    def get_thumbnail_path(self, file_path, asset_type, identifier, geometry_index=-1):
        key = self.asset_key(file_path, asset_type, identifier, geometry_index)
        return self._answer(('path',) + key, lambda: self._thumbnail_path(key))

    def _thumbnail_path(self, key):
        file_path, asset_type = key[0], key[1]
        if asset_type == 'Texture':
            texture = self.texture_thumbnail(file_path)
            if texture is None:
                return ''
            if not texture or os.path.isfile(texture):
                return texture or str(file_path)
            # The worker could not shrink it: the original still beats a blank card.
            if os.path.isfile(os.path.splitext(texture)[0] + FAILED_SUFFIX):
                return str(file_path)
            return ''
        path = self.paths.get(key, '')
        return path if path and png_has_visible_pixels(path) else ''

    def has_failed(self, file_path, asset_type, identifier, geometry_index=-1):
        key = self.asset_key(file_path, asset_type, identifier, geometry_index)
        return self._answer(('failed',) + key, lambda: self._failed(key))

    def _failed(self, key):
        if key[1] == 'Texture':
            texture = self.texture_thumbnail(key[0])
            return bool(texture and os.path.isfile(os.path.splitext(texture)[0] + FAILED_SUFFIX))
        path = self.paths.get(key, '')
        return bool(path and os.path.isfile(os.path.splitext(path)[0] + FAILED_SUFFIX))

    def accept(self, result):
        self.resource_roots = tuple(result['resource_roots'])
        self.paths.update((tuple(key), path) for key, path in result['paths'])
        self.jobs.update((job['output'], job) for job in result['jobs'])
        self.helper_state = result.get('helper_state', {})
        jobs = result['jobs']
        if len(jobs) <= LARGE_REQUEST:
            # The helper validated the PNGs it found; drawing need not decode them.
            missing = sum(1 for job in jobs if not remember_visible_png(job['output']))
        else:
            missing = len(jobs)
        self.needs_renders = missing > 0
        forget_lookups()

    def _texture_job(self, asset):
        texture = self.texture_thumbnail(asset.source_path)
        if (not texture or os.path.isfile(texture)
                or os.path.isfile(os.path.splitext(texture)[0] + FAILED_SUFFIX)):
            return None
        return {'file_path': os.path.abspath(str(asset.source_path)), 'asset_type': 'Texture',
                'identifier': str(getattr(asset, 'identifier', '') or ''), 'geometry_index': -1,
                'metadata_json': '', 'output': texture, 'texture_size': TEXTURE_THUMB_LIMIT}

    def ready_jobs(self, assets):
        assets = list(assets)
        check = len(assets) <= LARGE_REQUEST
        jobs = []
        for asset in assets:
            key = self.asset_key(asset.source_path, asset.asset_type, asset.identifier, asset.geometry_index)
            if asset.asset_type == 'Texture':
                job = self._texture_job(asset) if check else None
                if job:
                    jobs.append(job)
                continue
            path = self.paths.get(key)
            if path is None:
                return None
            if path in self.jobs and (not check or (not self.get_thumbnail_path(*key)
                                                     and not self.has_failed(*key))):
                jobs.append(self.jobs[path])
        return list({job['output']: job for job in jobs}.values())

    def write_queue(self, jobs, resource_pack='', queue_path=''):
        path = super().write_queue(jobs, resource_pack, queue_path)
        with open(path, encoding='utf-8') as stream:
            payload = json.load(stream)
        payload['browser_pack_only'] = True
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream)
        return path
