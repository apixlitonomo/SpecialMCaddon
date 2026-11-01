"""Find the add-on packs a Bedrock world uses, wherever Minecraft keeps them.

A world lists its behavior and resource packs by UUID in world_behavior_packs.json
and world_resource_packs.json. MiEx only reads packs stored inside the world
folder, but add-on makers keep theirs in com.mojang's development_*_packs (or
behavior_packs / resource_packs), so their custom blocks vanished on import:
the block definitions live in the behavior pack. Blender-free.
"""
import json
import os
from pathlib import Path
import zipfile

PACK_FOLDERS = ('development_resource_packs', 'resource_packs', 'development_behavior_packs', 'behavior_packs')


def mojang_roots(world, env=None):
    """com.mojang folders to search: the world's own, then every known install."""
    env = os.environ if env is None else env
    roots = []
    world = Path(world)
    if world.parent.name.lower() == 'minecraftworlds':
        roots.append(world.parent.parent)
    appdata, local = env.get('APPDATA'), env.get('LOCALAPPDATA')
    for base in filter(None, [appdata and Path(appdata) / 'Minecraft Bedrock' / 'Users',
                              appdata and Path(appdata) / 'Minecraft Bedrock Preview' / 'Users']):
        if base.is_dir():
            roots.extend(sorted(p / 'games' / 'com.mojang' for p in base.iterdir() if p.is_dir()))
    for package in ('Microsoft.MinecraftUWP_8wekyb3d8bbwe', 'Microsoft.MinecraftWindowsBeta_8wekyb3d8bbwe'):
        if local:
            roots.append(Path(local) / 'Packages' / package / 'LocalState' / 'games' / 'com.mojang')
    seen, result = set(), []
    for root in roots:
        key = os.path.normcase(str(root))
        if key not in seen and root.is_dir():
            seen.add(key)
            result.append(root)
    return result


def _read_json(text):
    # Pack manifests often carry comments or trailing commas.
    try:
        return json.loads(text)
    except ValueError:
        import re
        text = re.sub(r'//[^\n]*|/\*.*?\*/', '', text, flags=re.S)
        return json.loads(re.sub(r',(\s*[}\]])', r'\1', text))


def manifest(path):
    """(uuid, version list, module types, name) of a pack folder or .mcpack/.zip, or None."""
    path = Path(path)
    try:
        if path.is_dir():
            file = path / 'manifest.json'
            if not file.is_file():
                return None
            data = _read_json(file.read_text(encoding='utf-8-sig'))
        elif path.suffix.lower() in ('.mcpack', '.zip'):
            with zipfile.ZipFile(path) as archive:
                names = [n for n in archive.namelist() if n.lower().endswith('manifest.json') and n.count('/') <= 1]
                if not names:
                    return None
                data = _read_json(archive.read(sorted(names, key=len)[0]).decode('utf-8-sig'))
        else:
            return None
        header = data.get('header', {})
        modules = [m.get('type', '') for m in data.get('modules', []) if isinstance(m, dict)]
        return (str(header.get('uuid', '')).lower(), header.get('version', [0, 0, 0]), modules,
                str(header.get('name', path.name)))
    except (OSError, ValueError, zipfile.BadZipFile, AttributeError):
        return None


def encrypted(path):
    """Marketplace packs are encrypted; their files cannot be read."""
    path = Path(path)
    return path.is_dir() and (path / 'contents.json').is_file() and (path / 'signatures.json').is_file() and \
        not (path / 'textures').is_dir() and not (path / 'blocks').is_dir()


def world_addons(world, env=None):
    """Packs the world uses that MiEx does not load by itself, in priority order (first wins).

    Returns (entries, notes): entries are {'path', 'name', 'kind'} for prepare_packs.
    Packs inside the world folder are left to MiEx's own world dependencies."""
    world = Path(world)
    wanted = []
    for kind, listing in (('resources', 'world_resource_packs.json'), ('data', 'world_behavior_packs.json')):
        try:
            rows = _read_json((world / listing).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get('pack_id'):
                wanted.append((kind, str(row['pack_id']).lower(), row.get('version')))
    if not wanted:
        return [], []
    local = {}
    for folder in ('resource_packs', 'behavior_packs'):
        for pack in (world / folder).iterdir() if (world / folder).is_dir() else []:
            info = manifest(pack)
            if info:
                local[info[0]] = pack
    found = {}
    for root in mojang_roots(world, env):
        for folder in PACK_FOLDERS:
            base = root / folder
            if not base.is_dir():
                continue
            for pack in base.iterdir():
                info = manifest(pack)
                if not info:
                    continue
                # Prefer the exact version the world asks for, then the newest.
                found.setdefault(info[0], []).append((info[1], pack, info[3]))
    entries, notes = [], []
    for kind, uuid, version in wanted:
        if uuid in local:
            continue
        candidates = found.get(uuid, [])
        if not candidates:
            notes.append('Add-on pack %s is not installed on this computer' % uuid)
            continue
        exact = [c for c in candidates if version and list(c[0]) == list(version)]
        _, path, name = (exact or sorted(candidates, key=lambda c: list(c[0]), reverse=True))[0]
        if encrypted(path):
            notes.append('%s is an encrypted Marketplace pack; its blocks cannot be read' % name)
            continue
        if all(os.path.normcase(str(path)) != os.path.normcase(e['path']) for e in entries):
            entries.append({'path': str(path), 'name': name, 'kind': kind})
    return entries, notes
