"""Immutable effective pack views for Bedrock BR archives and subpacks."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
import gzip

from .brarchive_reader import _read_directory, _validated_name

SCHEMA = 'br-pack-v2'
_VIEWS = {}


def experiences(root):
    try:
        data = json.loads((Path(root) / 'manifest.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return []
    result = []
    for entry in data.get('subpacks', []):
        if not isinstance(entry, dict):
            continue
        folder = entry.get('folder_name', '')
        if folder and _validated_name(folder) == folder and '/' not in folder:
            result.append((folder, entry.get('name') or folder))
    return result


def selections(scene):
    try:
        return json.loads(scene.get('mcbedrock_pack_experiences', '{}')) if scene is not None else {}
    except (ValueError, AttributeError):
        return {}


def selected(root, choices=None):
    options = experiences(root)
    value = (choices or {}).get(os.path.normcase(os.path.abspath(root)), '')
    return value if value in dict(options) else (options[0][0] if options else '')


def has_archives(root):
    root = Path(root)
    return (root / '__brarchive').is_dir() or any(
        (root / 'subpacks' / name / '__brarchive').is_dir() for name, _ in experiences(root))


def clear_views():
    _VIEWS.clear()


def _files(root):
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in {'subpacks', '.git', '__pycache__'})
        for name in sorted(files):
            yield Path(current) / name


def _definitions(document):
    for kind in ('minecraft:client_entity', 'minecraft:attachable', 'particle_effect'):
        value = document.get(kind)
        if isinstance(value, dict):
            identifier = value.get('description', {}).get('identifier')
            if identifier:
                yield kind, identifier
    geometry = document.get('minecraft:geometry', [])
    if isinstance(geometry, list):
        for value in geometry:
            if not isinstance(value, dict):
                continue
            identifier = value.get('description', {}).get('identifier')
            if identifier:
                yield 'minecraft:geometry', identifier
    for kind in ('animations', 'animation_controllers', 'render_controllers'):
        if isinstance(document.get(kind), dict):
            for identifier in document[kind]:
                yield kind, identifier
    for key in document:
        if key.startswith('geometry.'):
            yield 'legacy_geometry', key


def _apply_definition_overrides(staging, written):
    """Subpack definitions may use a different filename for the same asset ID."""
    documents = {}
    overrides = set()
    for logical, layer in written.items():
        if not logical.lower().endswith('.json'):
            continue
        try:
            from .bedrock_materials import loads_material_jsonc
            data = loads_material_jsonc((staging / logical).read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                continue
            documents[logical] = data
            if layer:
                overrides.update(_definitions(data))
        except (OSError, ValueError, UnicodeError):
            continue
    for logical, data in documents.items():
        if written[logical]:
            continue
        replaced = set(_definitions(data)) & overrides
        if not replaced:
            continue
        for kind, identifier in replaced:
            if kind == 'minecraft:geometry':
                data[kind] = [v for v in data[kind] if v.get('description', {}).get('identifier') != identifier]
            elif kind in ('animations', 'animation_controllers', 'render_controllers'):
                data[kind].pop(identifier, None)
            elif kind == 'legacy_geometry':
                data.pop(identifier, None)
            else:
                data.pop(kind, None)
        target = staging / logical
        if list(_definitions(data)):
            target.write_text(json.dumps(data), encoding='utf-8')
        else:
            target.unlink()


def prepare_pack(root, experience=None, cache_dir=None):
    """Return (effective directory, diagnostics); original files are never changed."""
    root = Path(root).resolve()
    if not has_archives(root):
        return str(root), []
    experience = selected(root) if experience is None else experience
    if experience and experience not in dict(experiences(root)):
        raise ValueError('Unknown Pack Experience: ' + experience)
    key = (str(root), experience, str(cache_dir))
    if key in _VIEWS:
        return _VIEWS[key]
    layers = [root] + ([root / 'subpacks' / experience] if experience else [])
    inventories = [list(_files(layer)) for layer in layers]
    stamps = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for files in inventories for p in files]
    digest = hashlib.sha256(json.dumps([SCHEMA, experience, stamps]).encode()).hexdigest()[:24]
    if cache_dir is None:
        from .vanilla_assets import addon_cache_dir
        cache_dir = Path(addon_cache_dir()) / 'br'
    destination = Path(cache_dir) / digest
    marker = destination.with_suffix('.json')
    if marker.is_file():
        info = json.loads(marker.read_text(encoding='utf-8'))
        if (Path(info['view']) / '.br-source.json').is_file():
            _VIEWS[key] = (info['view'], info['diagnostics'])
            return _VIEWS[key]
    staging = destination.with_name(digest + '-' + uuid.uuid4().hex)
    staging.mkdir(parents=True)
    diagnostics = []
    written = {}
    try:
        for layer_index, (layer, files) in enumerate(zip(layers, inventories)):
            for path in files:
                rel = path.relative_to(layer)
                if '__brarchive' in rel.parts:
                    continue
                target = staging / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                written[rel.as_posix()] = layer_index
            for archive in files:
                rel = archive.relative_to(layer)
                if not rel.parts or rel.parts[0] != '__brarchive' or archive.suffix.lower() != '.brarchive':
                    continue
                try:
                    entries, start = _read_directory(archive)
                    folder = Path(*rel.parts[1:]).with_suffix('')
                    with archive.open('rb') as stream:
                        for entry in entries:
                            logical = folder / entry['name']
                            target = staging / logical
                            if not entry['size']:
                                if not target.is_file():
                                    diagnostics.append(f'{logical}: archive references a missing loose file')
                                continue
                            stream.seek(start + entry['offset'])
                            payload = stream.read(entry['size'])
                            if payload.startswith(b'\x7fMCB'):
                                diagnostics.append(f'{logical}: compiled MCB content is unsupported')
                                if target.is_file():
                                    target.unlink()
                                continue
                            if logical.suffix.lower() == '.json':
                                try:
                                    from .bedrock_materials import loads_material_jsonc
                                    loads_material_jsonc(payload.decode('utf-8-sig'))
                                except (ValueError, UnicodeError) as exc:
                                    diagnostics.append(f'{logical}: unreadable JSON ({exc})')
                                    if target.is_file():
                                        target.unlink()
                                    continue
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(payload)
                            written[logical.as_posix()] = layer_index
                except (OSError, ValueError) as exc:
                    diagnostics.append(f'{rel}: {exc}')
        _apply_definition_overrides(staging, written)
        (staging / '.br-source.json').write_text(json.dumps({
            'root': str(root), 'experience': experience, 'diagnostics': diagnostics}), encoding='utf-8')
        # Publish a small pointer atomically. Windows can deny renaming a large
        # freshly populated directory while its files are being scanned.
        pointer = staging.with_suffix('.json')
        pointer.write_text(json.dumps({'view': str(staging.resolve()), 'diagnostics': diagnostics}), encoding='utf-8')
        os.replace(pointer, marker)
    except Exception:
        if staging.is_dir():
            shutil.rmtree(staging)
        raise
    _VIEWS[key] = (str(staging.resolve()), diagnostics)
    return _VIEWS[key]


def resolve_roots(roots, choices=None):
    return [prepare_pack(root, selected(root, choices))[0] if has_archives(root) else root for root in roots]


def scan_pack(root, choices=None):
    """Index every experience, with one exact identity and per-experience sources."""
    from dataclasses import asdict
    from .asset_indexer import scan_asset_folder, BrowserAsset
    declared_options = experiences(root)
    options = declared_options or [('', 'Default')]
    chosen = selected(root, choices)
    union = {}
    issues = set()
    for option, label in options:
        view, diagnostics = prepare_pack(root, option)
        issues.update(diagnostics)
        index_path = Path(view).with_suffix('.index-v1.json.gz')
        try:
            with gzip.open(index_path, 'rt', encoding='utf-8') as stream:
                indexed = [BrowserAsset(**row) for row in json.load(stream)]
        except (OSError, ValueError, TypeError, EOFError):
            indexed = scan_asset_folder(view, include_archives=False)
            temporary = index_path.with_name(index_path.name + '.' + uuid.uuid4().hex)
            with gzip.open(temporary, 'wt', encoding='utf-8') as stream:
                json.dump([asdict(a) for a in indexed], stream, separators=(',', ':'))
            os.replace(temporary, index_path)
        for asset in indexed:
            logical = os.path.relpath(asset.source_path, view).replace('\\', '/')
            identity = (asset.asset_type, asset.identifier or logical)
            if asset.asset_type == 'Model':
                identity += (logical, asset.metadata.get('geometry_index', -1))
            variants = union.setdefault(identity, {})
            variants[option] = asdict(asset)
    result = []
    for identity, variants in union.items():
        row = variants.get(chosen) or next(iter(variants.values()))
        asset = BrowserAsset(**dict(row))
        asset.metadata = dict(asset.metadata)
        stable = hashlib.sha256(json.dumps([os.path.abspath(root), identity]).encode()).hexdigest()
        asset.metadata.update(br_root=os.path.abspath(root), br_identity=stable,
                              br_options=declared_options, br_variants=variants, br_available=chosen in variants)
        if chosen not in variants:
            asset.status = 'Choose Experience'
            asset.detail = 'Not available in the selected Pack Experience. Choose another experience in Import.'
        result.append(asset)
    for issue in sorted(issues):
        result.append(BrowserAsset(name='BR archive: ' + issue.split(':', 1)[0],
            asset_type='Archive Issue', source_path=str(root), pack_name=Path(root).name,
            status='Unsupported', detail=issue, importable=False))
    return result
