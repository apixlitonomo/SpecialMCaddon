"""Keep Blender material textures next to exports, independent of pack caches."""
import hashlib
import json
import os
from pathlib import Path
import shutil
from .service import MiExError


def preserve_textures(export, cancel):
    export = Path(export)
    sidecar = export.with_name(export.stem + '_materials.json')
    data = json.loads(sidecar.read_text(encoding='utf-8'))
    destinations = {}

    def visit(value):
        if isinstance(value, dict):
            if value.get('type') == 'asset' and isinstance(value.get('value'), str):
                source = Path(value['value'])
                if not source.is_absolute():
                    source = export.parent / source
                source = source.resolve()
                if not source.is_file():
                    raise MiExError('Export texture is missing: ' + str(source))
                if source not in destinations:
                    if cancel.is_set():
                        raise MiExError('Cancelled')
                    key = hashlib.sha256(str(source).encode()).hexdigest()[:16]
                    destination = export.parent / 'textures' / (key + '_' + source.name)
                    destination.parent.mkdir(exist_ok=True)
                    shutil.copy2(source, destination)
                    destinations[source] = destination.relative_to(export.parent).as_posix()
                value['value'] = destinations[source]
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(data)
    temporary = sidecar.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(temporary, sidecar)
    return len(destinations)
