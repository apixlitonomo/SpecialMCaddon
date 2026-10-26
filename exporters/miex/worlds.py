"""Known Minecraft launcher locations; never crawl unrelated user folders."""
import os
import gzip
import io
import struct
import zlib
from pathlib import Path


def launcher_roots(env=None, home=None):
    env = os.environ if env is None else env
    home = Path(home or Path.home())
    roaming = Path(env.get('APPDATA', home / '.config'))
    local = Path(env.get('LOCALAPPDATA', home / '.local/share'))
    roots = []
    seen = set()

    def add(label, kind, path):
        path = Path(path).expanduser()
        key = str(path.resolve()).casefold()
        if key not in seen and path.is_dir():
            seen.add(key)
            roots.append(dict(label=label, kind=kind, path=str(path.resolve())))

    for label, names in [('Prism Launcher', ['PrismLauncher', 'prismlauncher']),
                         ('MultiMC', ['MultiMC', 'multimc']), ('PolyMC', ['PolyMC', 'polymc'])]:
        for base in (roaming, local, home, home/'Applications', home/'Downloads', home/'Games',
                     home/'Library/Application Support'):
            for name in names:
                root = base/name
                if (root/'instances').is_dir():add(label, 'multimc', root)
    if env.get('MIEX_MULTIMC_ROOT_DIR'):add('MultiMC', 'multimc', env['MIEX_MULTIMC_ROOT_DIR'])
    for root in (roaming/'ModrinthApp', roaming/'com.modrinth.theseus', local/'ModrinthApp',
                 home/'Library/Application Support/com.modrinth.theseus'):
        if (root/'profiles').is_dir():add('Modrinth', 'modrinth', root)
    # These launchers use one Java game directory per instance.
    containers = [('CurseForge', home/'curseforge/minecraft/Instances'),
        ('CurseForge', home/'Documents/CurseForge/Minecraft/Instances'),
        ('CurseForge', roaming/'CurseForge/minecraft/Instances'),
        ('ATLauncher', roaming/'ATLauncher/instances'), ('ATLauncher', home/'ATLauncher/instances'),
        ('FTB', local/'ftb-instances'), ('FTB', home/'.ftba/instances'),
        ('GDLauncher', roaming/'gdlauncher_next/instances'), ('GDLauncher', roaming/'gdlauncher/instances')]
    for label, folder in containers:
        try:
            for instance in folder.iterdir():
                for game in (instance, instance/'minecraft', instance/'.minecraft'):
                    if (game/'saves').is_dir():add(label+' / '+instance.name, 'java', game)
        except OSError:
            continue
    return roots


def client_folders(env=None, home=None):
    """`versions/<id>/<id>.jar` folders whose client jars MiEx's launchers do not list."""
    env = os.environ if env is None else env
    home = Path(home or Path.home())
    roaming = Path(env.get('APPDATA', home / '.config'))
    candidates = [home/'curseforge/minecraft/Install/versions', home/'Documents/CurseForge/Minecraft/Install/versions',
                  roaming/'CurseForge/minecraft/Install/versions', roaming/'.minecraft/versions',
                  home/'.minecraft/versions', home/'Library/Application Support/minecraft/versions']
    seen, result = set(), []
    for folder in candidates:
        if folder.is_dir() and str(folder.resolve()).casefold() not in seen:
            seen.add(str(folder.resolve()).casefold())
            result.append(str(folder.resolve()))
    return result


def normalize_worlds(rows):
    """Preserve launcher identity, omit Hytale, and deduplicate actual save paths."""
    result = {}
    for row in rows:
        if 'hytale' in row.get('launcher', '').casefold() or not row.get('path'):
            continue
        key = os.path.normcase(os.path.abspath(row['path'])).casefold()
        result[key] = dict(name=row.get('name') or Path(row['path']).name,
                           path=row['path'], launcher=row.get('launcher', 'Minecraft'))
    for row in result.values():
        row.update(world_details(row))
    # Saves may share a LevelName; retain the folder label that distinguishes them.
    groups = {}
    for row in result.values():
        groups.setdefault((row['display_name'], row['launcher'], row['instance']), []).append(row)
    for rows in groups.values():
        if len(rows) > 1:
            for row in rows:
                label = Path(row['path']).name
                if label != row['display_name']:
                    row['display_name'] = label if label.startswith(row['display_name']) else row['display_name']+' · '+label
    return sorted(result.values(), key=lambda r:(r['launcher'].casefold(), r['name'].casefold()))


def _level_metadata(path, bedrock):
    """Read only a bounded level.dat; never open a world database or session lock."""
    limit = 8 * 1024 * 1024
    with path.open('rb') as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError('level.dat is too large')
    endian = '<' if bedrock else '>'
    if bedrock:
        data = data[8:]
    elif data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Expanded level.dat is too large')
    stream = io.BytesIO(data)

    def read(size):
        value = stream.read(size)
        if len(value) != size:
            raise ValueError('Truncated level.dat')
        return value

    def number(code):
        return struct.unpack(endian + code, read(struct.calcsize(code)))[0]

    def string():
        return read(number('H')).decode('utf-8', errors='replace')

    def payload(kind, depth=0):
        if depth > 64:
            raise ValueError('NBT nesting limit')
        if 1 <= kind <= 6:
            return number({1:'b', 2:'h', 3:'i', 4:'q', 5:'f', 6:'d'}[kind])
        if kind == 8:
            return string()
        if kind == 10:
            values = {}
            while True:
                child = number('B')
                if child == 0:
                    return values
                name = string()
                values[name] = payload(child, depth + 1)
        if kind in (7, 9, 11, 12):
            child = number('B') if kind == 9 else {7:1, 11:3, 12:4}[kind]
            count = number('i')
            if count < 0 or count > 1000000:
                raise ValueError('NBT array limit')
            if kind != 9:
                read(count * {7:1, 11:4, 12:8}[kind])
                return None
            return [payload(child, depth + 1) for _ in range(count)]
        raise ValueError('Unsupported NBT tag')

    if number('B') != 10:
        raise ValueError('Invalid NBT root')
    string()
    root = payload(10)
    return root if bedrock else root.get('Data', root)


def world_details(row):
    folder = Path(row['path'])
    bedrock = (folder/'db').is_dir() or 'bedrock' in row.get('launcher', '').lower()
    icon_names = ('world_icon.jpeg', 'world_icon.jpg', 'world_icon.png') if bedrock else ('icon.png',)
    details = dict(display_name=row['name'].rsplit('/', 1)[-1], edition='Bedrock' if bedrock else 'Java',
                   version='', icon_path=next((str(folder/n) for n in icon_names if (folder/n).is_file()), ''),
                   instance=row['name'].rsplit('/', 1)[0] if '/' in row['name'] else '')
    try:
        data = _level_metadata(folder/'level.dat', bedrock)
        if isinstance(data.get('LevelName'), str) and data['LevelName'].strip():
            details['display_name'] = data['LevelName'].strip()
        if bedrock:
            version = data.get('lastOpenedWithVersion', [])
            if isinstance(version, list) and len(version) >= 3 and all(isinstance(v, int) for v in version):
                details['version'] = '.'.join(map(str, version[:3]))
        else:
            version = data.get('Version', {})
            if isinstance(version, dict) and isinstance(version.get('Name'), str):
                details['version'] = version['Name']
    except (OSError, ValueError, EOFError, struct.error, AttributeError, zlib.error):
        pass  # Unreadable metadata must not hide an otherwise selectable save.
    return details


def dimension_key(name):
    name=name.replace(':','/').removeprefix('minecraft/')
    return {'DIM-1':'the_nether','DIM1':'the_end'}.get(name,name)
