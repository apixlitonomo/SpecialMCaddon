"""UI-independent pack inspection and query model. Never extracts pack payloads.

Call inspect_pack in a worker, or use FeatureIndex from a UI thread. JSON reads
are capped; PNG resolution uses only the 24-byte signature/IHDR prefix.
"""
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
import re
import struct
import zipfile

CORE = (('blocks', 'Has Blocks'), ('items', 'Has Items'), ('gui', 'Has GUI/UI'),
        ('audio', 'Has Custom Audio'), ('models', 'Has Models'), ('particles', 'Particles'))
ADVANCED = (('pbr', 'PBR / RTX'), ('animated', 'Animated Textures'), ('subpacks', 'Subpacks'),
            ('cem', 'OptiFine CEM'), ('cit', 'OptiFine CIT'), ('ctm', 'OptiFine CTM'))
SORTS = (('library', 'Library Order'), ('name', 'Name'), ('size', 'File Size'), ('version', 'Target Game Version'),
         ('added', 'Date Added'), ('modified', 'Date Modified'), ('resolution', 'Estimated Resolution'))
# Release-format mapping; unknown/snapshot formats remain unknown, never guessed.
# Modern format syntax: minecraft.net/en-us/article/minecraft-java-edition-1-21-9
# 75/84: minecraft.net/en-us/article/minecraft-java-edition-1-21-11 and -26-1
JAVA_FORMATS = {1: ((1,6,1),(1,8,9)), 2: ((1,9,0),(1,10,2)),
    3: ((1,11,0),(1,12,2)), 4: ((1,13,0),(1,14,4)), 5: ((1,15,0),(1,16,1)),
    6: ((1,16,2),(1,16,5)), 7: ((1,17,0),(1,17,1)), 8: ((1,18,0),(1,18,2)),
    9: ((1,19,0),(1,19,2)), 12: ((1,19,3),(1,19,3)), 13: ((1,19,4),(1,19,4)),
    15: ((1,20,0),(1,20,1)), 18: ((1,20,2),(1,20,2)), 22: ((1,20,3),(1,20,4)),
    32: ((1,20,5),(1,20,6)), 34: ((1,21,0),(1,21,1)), 42: ((1,21,2),(1,21,3)),
    46: ((1,21,4),(1,21,4)), 55: ((1,21,5),(1,21,5)), 63: ((1,21,6),(1,21,6)),
    64: ((1,21,7),(1,21,8)), 69: ((1,21,9),(1,21,10)), 75: ((1,21,11),(1,21,11)),
    84: ((26,1,0),(26,1,2)), 88: ((26,2,0),(26,2,0))}

def version(value):
    if isinstance(value, str):
        if not re.fullmatch(r'\d+(?:\.\d+){0,2}', value.strip()):
            return None
        value = [int(v) for v in value.strip().split('.')]
    if isinstance(value, (list, tuple)) and 1 <= len(value) <= 3 and all(type(v) is int and v >= 0 for v in value):
        return tuple(value) + (0,) * (3-len(value))
    return None

def _format(value):
    if type(value) is int and value >= 0:
        return (value, 0)
    if isinstance(value, list) and len(value) == 2 and all(type(v) is int and v >= 0 for v in value):
        return tuple(value)
    return None

@dataclass
class PackFeatures:
    path: str
    flags: set = field(default_factory=set)
    edition: str = 'unknown'
    size: int | None = None
    added: float | None = None
    modified: float | None = None
    version_min: tuple | None = None
    version_max: tuple | None = None
    format_min: tuple | None = None
    format_max: tuple | None = None
    resolution: int | None = None
    resolution_source: str = ''
    errors: list = field(default_factory=list)

@dataclass
class PackQuery:
    include: set = field(default_factory=set)
    exclude: set = field(default_factory=set)
    match_all: bool = False
    minimum: tuple | None = None
    maximum: tuple | None = None
    sort: str = 'library'
    descending: bool = False

def matches(features, query):
    if features is None:
        return not (query.include or query.exclude or query.minimum or query.maximum)
    if query.exclude & features.flags:
        return False
    if query.include and not (query.include <= features.flags if query.match_all else query.include & features.flags):
        return False
    if query.minimum:
        # Bedrock declares a minimum, not an upper compatibility bound.
        if features.edition == 'bedrock':
            if features.version_min is None:
                return False
        elif features.version_max is None or features.version_max < query.minimum:
            return False
    if query.maximum and (features.version_min is None or features.version_min > query.maximum):
        return False
    return True

def order_entries(entries, features, query):
    """Return original (index, item) pairs; unknown metadata always sorts last."""
    entries = [e for e in entries if matches(features.get(e[1].path), query)]
    if query.sort == 'library':
        return list(reversed(entries)) if query.descending else entries
    def value(entry):
        item = entry[1]
        if query.sort == 'name':
            return item.name.casefold()
        data = features.get(item.path)
        if data is None:
            return None
        return (data.version_max or data.version_min) if query.sort == 'version' else getattr(data, query.sort, None)
    entries.sort(key=lambda e: (e[1].name.casefold(), e[1].path.casefold(), e[0]))
    known = [e for e in entries if value(e) is not None]
    unknown = [e for e in entries if value(e) is None]
    return sorted(known, key=value, reverse=query.descending) + unknown

def inspect_pack(path):
    result = PackFeatures(path)
    try:
        stat = os.stat(path)
        result.added = getattr(stat, 'st_birthtime', stat.st_ctime if os.name == 'nt' else None)
        result.modified = stat.st_mtime
        with ExitStack() as stack:
            if os.path.isdir(path):
                entries = {}
                result.size = 0
                def walk_error(exc):
                    result.errors.append(str(exc))
                for root, dirs, files in os.walk(path, onerror=walk_error):
                    dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                    for name in files:
                        full = os.path.join(root, name)
                        if os.path.islink(full):
                            continue
                        try:
                            st = os.stat(full)
                            result.size += st.st_size
                            result.modified = max(result.modified, st.st_mtime)
                            entries[os.path.relpath(full, path).replace('\\', '/').lower()] = full
                        except OSError as exc:
                            result.errors.append(str(exc))
                def read(name, limit):
                    with open(entries[name], 'rb') as stream:
                        return stream.read(limit)
            else:
                archive = stack.enter_context(zipfile.ZipFile(path))
                entries = {i.filename.replace('\\', '/').lower(): i for i in archive.infolist() if not i.is_dir()}
                result.size = stat.st_size
                def read(name, limit):
                    with archive.open(entries[name]) as stream:
                        return stream.read(limit)
            # Support a single wrapper folder without mixing unrelated pack roots.
            roots = [n.rsplit('/', 1)[0]+'/' if '/' in n else '' for n in entries
                     if n.rsplit('/', 1)[-1] in ('pack.mcmeta', 'manifest.json')]
            prefix = min(roots, key=lambda p: (p.count('/'), len(p), p)) if roots else ''
            names = {n[len(prefix):]: n for n in entries if n.startswith(prefix)}
            def metadata(name):
                if name not in names:
                    return {}
                try:
                    raw = read(names[name], 2*1024*1024+1)
                    if len(raw) > 2*1024*1024:
                        raise ValueError('Metadata exceeds 2 MiB')
                    obj = json.loads(raw.decode('utf-8-sig'))
                    return obj if isinstance(obj, dict) else {}
                except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    result.errors.append(name+': '+str(exc))
                    return {}
            java, bedrock = metadata('pack.mcmeta'), metadata('manifest.json')
            if java:
                result.edition = 'java'
                pack = java.get('pack', {})
                if not isinstance(pack, dict):
                    pack = {}
                lo = hi = pack.get('pack_format')
                supported = pack.get('supported_formats')
                if type(supported) is int:
                    lo = hi = supported
                elif isinstance(supported, list) and len(supported) == 2:
                    lo, hi = supported
                elif isinstance(supported, dict):
                    lo, hi = supported.get('min_inclusive', lo), supported.get('max_inclusive', hi)
                result.format_min = _format(pack.get('min_format', lo))
                result.format_max = _format(pack.get('max_format', hi))
                if result.format_min and result.format_max and result.format_min <= result.format_max:
                    result.version_min = JAVA_FORMATS.get(result.format_min[0], (None, None))[0]
                    result.version_max = JAVA_FORMATS.get(result.format_max[0], (None, None))[1]
            elif bedrock:
                result.edition = 'bedrock'
                header = bedrock.get('header', {})
                result.version_min = version(header.get('min_engine_version')) if isinstance(header, dict) else None
                if isinstance(bedrock.get('subpacks'), list) and bedrock['subpacks']:
                    result.flags.add('subpacks')
            samples = []
            for name, original in names.items():
                # Bedrock subpack assets and Java overlay assets share the map.
                logical = re.sub(r'^subpacks/[^/]+/', '', name)
                if '/assets/' in logical:
                    logical = 'assets/' + logical.split('/assets/', 1)[1]
                logical = re.sub(r'^assets/[^/]+/', '', logical)
                paths = {'blocks': ('textures/block/', 'textures/blocks/'),
                         'items': ('textures/item/', 'textures/items/'),
                         'gui': ('textures/gui/', 'textures/ui/', 'ui/'),
                         'audio': ('sounds/',), 'models': ('models/', 'attachables/'),
                         'particles': ('textures/particle/', 'textures/particles/'),
                         'cem': ('optifine/cem/',), 'cit': ('optifine/cit/', 'cit/'),
                         'ctm': ('optifine/ctm/',)}
                for flag, prefixes in paths.items():
                    if logical.startswith(prefixes):
                        result.flags.add(flag)
                if logical in ('sounds.json', 'sound_definitions.json'):
                    result.flags.add('audio')
                if logical.endswith(('_n.png', '_s.png', '_mer.png', '.texture_set.json')):
                    result.flags.add('pbr')
                if logical.endswith('.png.mcmeta') or logical == 'textures/flipbook_textures.json':
                    result.flags.add('animated')
                if logical.startswith(('textures/block/', 'textures/blocks/')) and logical.endswith('.png') and not logical.endswith(('_n.png', '_s.png', '_mer.png')):
                    priority = {'dirt.png': 0, 'stone.png': 1, 'grass_block_top.png': 2, 'grass_top.png': 2}.get(logical.rsplit('/', 1)[-1], 3)
                    samples.append((priority, original))
            for _, name in sorted(samples)[:8]:
                try:
                    header = read(name, 24)
                    if header[:8] == b'\x89PNG\r\n\x1a\n' and header[12:16] == b'IHDR' and len(header) == 24:
                        width, height = struct.unpack('>II', header[16:24])
                        if width > 0 and height > 0:
                            result.resolution = min(width, height)
                            result.resolution_source = name
                            break
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    result.errors.append(str(exc))
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
        result.errors.append(str(exc))
    return result

class FeatureIndex:
    """Bounded worker queue; only strings cross into workers, never Blender RNA.

    Refresh on library scan revision, including edits deep inside folder packs.
    Poll results on the UI thread. No filesystem operations happen in snapshot.
    """
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='pack-features')
        self.results, self.pending = {}, {}
        self.generation = None

    def snapshot(self, paths, generation=None):
        paths = set(paths)
        if generation != self.generation:
            for future in self.pending.values():
                future.cancel()
            self.pending.clear()
            self.results.clear()
            self.generation = generation
        for path, future in list(self.pending.items()):
            if future.done():
                try:
                    self.results[path] = future.result()
                except Exception as exc:
                    self.results[path] = PackFeatures(path, errors=[str(exc)])
                del self.pending[path]
        for path in self.results.keys() - paths:
            del self.results[path]
        for path in sorted(paths - self.results.keys() - self.pending.keys())[:max(0, 4-len(self.pending))]:
            self.pending[path] = self.pool.submit(inspect_pack, path)
        return self.results, len(paths - self.results.keys())

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
