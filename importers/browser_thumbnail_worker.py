"""Resolve thumbnail dependencies off the interactive Blender thread."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from importers.bedrock_assets import asset_resource_roots
from importers.thumbnail_cache import ThumbnailCache
from importers.browser_thumbnail_cache import BrowserThumbnailCache


def prepare(request):
    from importers.thumbnail_cache import _DEPENDENCY_FINGERPRINT_CACHE
    from importers.brarchive_pack import _VIEWS
    state = request.get('helper_state', {})
    _DEPENDENCY_FINGERPRINT_CACHE.update(state.get('fingerprints', {}))
    _DEPENDENCY_FINGERPRINT_CACHE.update({('stack', key): value for key, value in state.get('stacks', [])})
    _VIEWS.update({tuple(key): tuple(value) for key, value in state.get('views', [])})
    class Scene(dict):
        pass
    scene = Scene(mcbedrock_pack_experiences=json.dumps(request['choices']),
                  _mcbedrock_asset_browser_root=request['root'])
    scene.mcbedrock_active_packs = [SimpleNamespace(path=p) for p in request['resource_roots']]
    scene.mcbedrock_resource_pack_path = request['root']
    roots = asset_resource_roots(SimpleNamespace(scene=scene))
    cache = ThumbnailCache(request['cache_dir'], request['size'], roots, request['vanilla'])
    paths = []
    for asset in request['assets']:
        key = BrowserThumbnailCache.asset_key(asset['source_path'], asset['asset_type'],
                                             asset['identifier'], asset.get('geometry_index', -1))
        paths.append((key, cache.get_cache_path(*key)))
    jobs = cache.build_queue(request['assets'])
    # Retain a descriptor for existing PNGs too, so deleting a cache file can
    # regenerate it without another dependency scan.
    queued = {job['output'] for job in jobs}
    for asset, (key, path) in zip(request['assets'], paths):
        if asset['asset_type'] != 'Texture' and path not in queued and cache.get_thumbnail_path(*key):
            jobs.append({'file_path': asset['source_path'], 'asset_type': asset['asset_type'],
                         'identifier': asset['identifier'], 'geometry_index': asset.get('geometry_index', -1),
                         'metadata_json': asset.get('metadata_json', ''), 'output': path})
    return {'resource_roots': roots, 'paths': paths, 'jobs': jobs,
            'helper_state': {
                'fingerprints': {k: v for k, v in _DEPENDENCY_FINGERPRINT_CACHE.items() if isinstance(k, str)},
                'stacks': [[k[1], v] for k, v in _DEPENDENCY_FINGERPRINT_CACHE.items() if isinstance(k, tuple)],
                'views': [[list(k), v] for k, v in _VIEWS.items()]}}


if __name__ == '__main__':
    request = json.loads(Path(sys.argv[sys.argv.index('--') + 1]).read_text(encoding='utf-8'))
    try:
        result = prepare(request)
    except Exception as exc:
        result = {'error': str(exc)}
    Path(request['output']).write_text(json.dumps(result), encoding='utf-8')
