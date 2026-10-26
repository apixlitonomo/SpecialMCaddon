"""Run only in an isolated process (Blender's bundled Python); never touch the user's scene."""
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from importers.browser_index import scan_browser_assets
from importers.thumbnail_cache import _dependency_fingerprint, _stack_dependency_fingerprint, _DEPENDENCY_FINGERPRINT_CACHE

request_path = Path(sys.argv[sys.argv.index('--')+1])
request = json.loads(request_path.read_text(encoding='utf-8'))
try:
    assets = scan_browser_assets(request['root'], request['vanilla'], request.get('choices'))
    seen = set()
    for asset in assets:
        key = (str(Path(asset.source_path).parent), asset.asset_type)
        if key not in seen:
            seen.add(key)
            _dependency_fingerprint(asset.source_path, asset.asset_type)
    # The first preview must not walk the complete dependency tree on Blender's
    # drawing thread. Prepare both source and active-stack fingerprints here.
    from importers.brarchive_pack import resolve_roots, _VIEWS
    from importers.bedrock_assets import _stack_roots_for_lookup
    roots = [child for root in request.get('resource_roots', [request['root']])
             for child in (_stack_roots_for_lookup(root) or [root])]
    roots = resolve_roots(roots, request.get('choices'))
    _stack_dependency_fingerprint([*roots, request['vanilla']])
    result = {'assets': [asdict(asset) for asset in assets],
              'fingerprints': {key: value for key, value in _DEPENDENCY_FINGERPRINT_CACHE.items()
                               if isinstance(key, str)},
              'stack_fingerprints': [[key[1], value] for key, value in _DEPENDENCY_FINGERPRINT_CACHE.items()
                                     if isinstance(key, tuple) and key[0] == 'stack'],
              'prepared_views': [[list(key), value] for key, value in _VIEWS.items()]}
except Exception as exc:
    result = {'error': str(exc)}
Path(request['output']).write_text(json.dumps(result), encoding='utf-8')
