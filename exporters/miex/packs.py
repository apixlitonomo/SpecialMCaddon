"""Prepare MiEx folder packs without touching the source library."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
from .service import MiExError


def pack_roots(path):
    path = Path(path)
    if any((path / name).exists() for name in ('pack.mcmeta', 'manifest.json', 'assets', 'textures')):
        return [path]
    result = []
    for root, dirs, files in os.walk(path):
        if len(Path(root).relative_to(path).parts) > 5:
            dirs[:] = []
            continue
        if 'pack.mcmeta' in files or 'manifest.json' in files or 'assets' in dirs:
            result.append(Path(root))
            dirs[:] = []
    return sorted(result)


def own_identity(folder, key):
    """Give a prepared copy of a Bedrock pack its own manifest UUID.

    MiEx keys packs by that UUID. Copies left by earlier sessions, a world's own
    packs (merged into mcaddon_world_dependencies) and the user's stack can share
    one, and MiEx then activates the wrong folder: a behavior pack without its
    resource pack's geometry sent MiEx's command thread into a stack overflow.
    Only the session copy changes; the original UUID is kept beside it."""
    from .bedrock_packs import _read_json
    manifest = Path(folder) / 'manifest.json'
    if not manifest.is_file():
        return
    try:
        data = _read_json(manifest.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return
    header = data.get('header') if isinstance(data, dict) else None
    if not isinstance(header, dict):
        return
    own = str(uuid.uuid5(uuid.NAMESPACE_URL, 'sparkle-miex:' + key))
    if header.get('uuid') != own:
        header.setdefault('mcaddon_source_uuid', header.get('uuid'))
        header['uuid'] = own
        manifest.write_text(json.dumps(data, indent=2), encoding='utf-8')


def prepare_packs(entries, resources, cache, cancel, progress=None, labels=None):
    """``progress(text)`` hears about first-time copies, which take a minute for a
    20,000-file server pack and otherwise look like a hang. ``labels`` (a list)
    receives (pack name, prepared folder) for each folder returned."""
    from ...importers.archive_inputs import extract_archive_result
    resources = Path(resources)
    resources.mkdir(parents=True, exist_ok=True)
    # Copies made before packs got their own identity must not shadow today's.
    for folder in resources.glob('mcaddon_*'):
        if folder.name[len('mcaddon_'):].isalnum() and len(folder.name) == len('mcaddon_') + 20:
            own_identity(folder, folder.name)
    prepared = []
    for entry in entries:
        if cancel.is_set():
            raise MiExError('Cancelled')
        source = Path(entry['path'])
        if not source.exists():
            raise MiExError('Pack is missing: ' + str(source))
        if source.is_file():
            result = extract_archive_result(str(source), str(cache))
            if not result.get('output_path'):
                raise MiExError(result.get('error') or 'Cannot extract ' + source.name)
            roots = pack_roots(result['output_path'])
        else:
            roots = pack_roots(source)
        if not roots:
            raise MiExError('No usable pack folders in ' + source.name)
        for root in roots:
            digest = hashlib.sha256(str(root.resolve()).encode())
            for file in sorted(root.rglob('*')):
                if cancel.is_set():
                    raise MiExError('Cancelled')
                if file.is_file():
                    stat = file.stat()
                    digest.update(('%s:%s:%s' % (file.relative_to(root), stat.st_size, stat.st_mtime_ns)).encode())
            key = 'mcaddon_' + digest.hexdigest()[:20]
            destination = resources / key
            if not destination.exists():
                staging = resources.parent / ('staging_' + key)
                staging.mkdir(exist_ok=True)
                label = entry.get('name') or source.name
                copied = [0]
                def copy(src, dst):
                    if cancel.is_set():
                        raise MiExError('Cancelled')
                    copied[0] += 1
                    if progress and copied[0] % 2000 == 1:
                        progress('Preparing %s for MiEx (%d files copied)…' % (label, copied[0] - 1))
                    return shutil.copy2(src, dst)
                shutil.copytree(root, staging, dirs_exist_ok=True, copy_function=copy)
                for attempt in range(4):
                    try:
                        os.replace(staging, destination)
                        break
                    except PermissionError:
                        if attempt == 3:raise
                        if cancel.wait(.1 * (attempt+1)):raise MiExError('Cancelled')
            own_identity(destination, key)
            prepared.append(str(destination.resolve()))
            if labels is not None:
                labels.append((entry.get('name') or source.name, prepared[-1]))
    return prepared


def prepare_template(resources, engine):
    name = 'BlenderCycles' if engine == 'CYCLES' else 'BlenderEEVEE'
    destination = Path(resources) / name
    shutil.copytree(Path(__file__).with_name('templates') / name, destination, dirs_exist_ok=True)
    return str(destination.resolve())


FIXED_FOLIAGE_TINTS = {
    # These leaves keep their own colour in every biome. The generated MiEx
    # base can incorrectly assign biome foliage tint to a shared leaf model.
    'minecraft:cherry_leaves': '#FFFFFF',
    'minecraft:azalea_leaves': '#FFFFFF',
    'minecraft:flowering_azalea_leaves': '#FFFFFF',
    'minecraft:pale_oak_leaves': '#FFFFFF',
    'minecraft:red_poplar_leaves': '#FFFFFF',
    'minecraft:orange_poplar_leaves': '#FFFFFF',
    'minecraft:yellow_poplar_leaves': '#FFFFFF',
    'minecraft:spruce_leaves': '#619961',
    'minecraft:birch_leaves': '#80A755',
}

BIOME_FOLIAGE_LEAVES = {
    'minecraft:oak_leaves', 'minecraft:dark_oak_leaves',
    'minecraft:jungle_leaves', 'minecraft:mangrove_leaves',
    'minecraft:acacia_leaves',
}


def prepare_biome_defaults(resources):
    """Correct known vanilla tint rules below user packs and above MiEx base."""
    destination = Path(resources) / 'mcaddon_biome_defaults'
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'pack.mcmeta').write_text(json.dumps({'pack': {
        'pack_format': 15, 'description': 'Sparkle vanilla biome defaults'}}), encoding='utf-8')
    tints={
        block: {'tint': 'minecraft:water'} for block in
        ('minecraft:water', 'minecraft:flowing_water', 'minecraft:water_cauldron')
    }
    tints.update({block:{'tint':colour} for block,colour in FIXED_FOLIAGE_TINTS.items()})
    base = Path(resources) / 'base_resource_pack' / 'miex_block_tints.json'
    if base.is_file():
        try:
            base_tints = json.loads(base.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            base_tints = {}
        for block, rule in base_tints.items():
            if (block.startswith('minecraft:') and block.endswith('_leaves')
                    and block not in BIOME_FOLIAGE_LEAVES
                    and isinstance(rule, dict) and rule.get('tint') == 'minecraft:foliage'):
                tints.setdefault(block, {'tint': '#FFFFFF'})
    (destination / 'miex_block_tints.json').write_text(json.dumps(tints), encoding='utf-8')
    # MiEx built-in models corrected to Minecraft's own placement (item frames:
    # an invisible frame's item lies flush with the wall). MiEx reads builtins/
    # from every active pack, the highest winning.
    shutil.copytree(Path(__file__).with_name('templates') / 'JavaDefaults', destination, dirs_exist_ok=True)
    return str(destination.resolve())


def prepare_bedrock_compat(resources):
    """Sparkle's Bedrock tables (tools/build_bedrock_compat.py) above MiEx's own.

    They add Bedrock 26.40+ stored corners and connections, newer blocks and
    biome ids, and Java's neighbour rules where a world stores no result.
    MiEx's jar stays unmodified; a higher pack replaces a block's entry."""
    destination = Path(resources) / 'mcaddon_bedrock_compat'
    shutil.copytree(Path(__file__).with_name('bedrock_compat'), destination, dirs_exist_ok=True)
    return str(destination.resolve())


def prepare_liquid_models(resources):
    """Keep vanilla particle-only liquid models from hiding MiEx fluid meshes.

    These blockstates provide particles, not fluid geometry. MiEx treats their
    presence as a replacement for its built-in liquid mesher. Work only on
    generated session packs; original source packs remain untouched. Preserve
    custom models with parents, elements or non-particle texture declarations.
    """
    repaired = []
    for pack in Path(resources).iterdir():
        for liquid in ('water', 'lava'):
            blockstate = pack / f'assets/minecraft/blockstates/{liquid}.json'
            model = pack / f'assets/minecraft/models/block/{liquid}.json'
            if not blockstate.is_file() or not model.is_file():
                continue
            try:
                state = json.loads(blockstate.read_text(encoding='utf-8'))
                data = json.loads(model.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            variants = state.get('variants')
            if (set(state) == {'variants'} and isinstance(variants, dict)
                    and set(variants) == {''} and variants[''] in (
                        {'model': f'minecraft:block/{liquid}'}, {'model': f'block/{liquid}'})
                    and set(data) == {'textures'} and isinstance(data['textures'], dict)
                    and set(data['textures']) == {'particle'}):
                blockstate.unlink()
                repaired.append(str(blockstate))
    return repaired


def resolve_ids(prepared, rows):
    by_path = {os.path.normcase(os.path.abspath(r['folder'])): r['uuid'] for r in rows}
    result = []
    for path in prepared:
        key = os.path.normcase(os.path.abspath(path))
        if key not in by_path:
            raise MiExError('MiEx did not recognize prepared pack: ' + path)
        uuid = by_path[key]
        if uuid in result:
            raise MiExError('Selected packs have duplicate MiEx identities: ' + uuid)
        result.append(uuid)
    return result


# --- Java worlds: decorations, the world's own biomes, stale dependencies, problems ---

# In Minecraft these are part of a build, not mobs: they export with the area even
# when Entities is off. MiEx draws frames, paintings and displays into the chunk
# meshes itself (exportEntityAsBlocks); armour stands need the entity list.
DECORATION_ENTITIES = ('minecraft:armor_stand', 'minecraft:item_frame', 'minecraft:glow_item_frame',
                       'minecraft:painting', 'minecraft:item_display', 'minecraft:block_display')


def entity_exports(entities, types, defaults):
    """MiEx's entityExport list: the chosen (or default) entities when Entities is
    on, and the decorations always."""
    chosen = list(types or defaults or []) if entities else []
    return chosen + [name for name in DECORATION_ENTITIES if name not in chosen]


def setting_applied(key, requested, effective):
    """Whether MiEx applied an export setting. MiEx keeps entity lists sorted and
    drops ids it cannot offer (a type with none in the area), so those match as sets,
    with every requested decoration still there."""
    if key == 'entityExport' and isinstance(effective, list) and isinstance(requested, list):
        return set(effective) <= set(requested) and set(DECORATION_ENTITIES) & set(requested) <= set(effective)
    if key in ('entitySimulate', 'entitySpawnRules') and isinstance(effective, list) and isinstance(requested, list):
        return set(effective) <= set(requested)
    return effective == requested


def _biome_entry(name):
    parts = name.split('/')
    return (len(parts) >= 5 and parts[0] == 'data' and parts[2] == 'worldgen' and parts[3] == 'biome'
            and name.endswith('.json'))


def world_biome_files(world):
    """{data/<ns>/worldgen/biome/... : bytes} from the world's datapacks folders and zips."""
    import zipfile
    files = {}
    folder = Path(world) / 'datapacks'
    for source in sorted(folder.iterdir()) if folder.is_dir() else ():
        if source.is_dir():
            for path in sorted(source.glob('data/*/worldgen/biome/**/*.json')):
                files[path.relative_to(source).as_posix()] = path.read_bytes()
        elif source.suffix.lower() in ('.zip', '.jar'):
            try:
                with zipfile.ZipFile(source) as archive:
                    for name in archive.namelist():
                        if _biome_entry(name):
                            files[name] = archive.read(name)
            except (OSError, zipfile.BadZipFile):
                continue
    return files


def prepare_world_biomes(world, resources):
    """A pack with the biome definitions of the world's own datapacks, every namespace.

    MiEx reads biome colours from data/<ns>/worldgen/biome in the active packs and
    adds a world's datapacks/ itself, but only folders that contain
    data/minecraft/worldgen/biome, so a server's own biomes (originrealms:foothills)
    were skipped and drew grey. Returns the pack folder, or None (and removes the
    pack) when the world has no biome definitions.
    """
    destination = Path(resources) / 'mcaddon_world_biomes'
    files = world_biome_files(world)
    if not files:
        shutil.rmtree(destination, ignore_errors=True)
        return None
    digest = hashlib.sha256(json.dumps(sorted((name, hashlib.sha256(data).hexdigest())
                                              for name, data in files.items())).encode()).hexdigest()
    stamp = destination / 'mcaddon_world_biomes.json'
    try:
        if json.loads(stamp.read_text(encoding='utf-8')).get('digest') == digest:
            return str(destination.resolve())
    except (OSError, ValueError, AttributeError):
        pass
    shutil.rmtree(destination, ignore_errors=True)
    for name, data in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (destination / 'pack.mcmeta').write_text(json.dumps({'pack': {
        'pack_format': 15, 'description': "Biomes of the world's own datapacks"}}), encoding='utf-8')
    stamp.write_text(json.dumps({'digest': digest, 'world': str(world), 'biomes': len(files)}), encoding='utf-8')
    return str(destination.resolve())


def _world_key(world):
    return os.path.normcase(os.path.abspath(str(world)))


def reset_world_dependencies(resources, world):
    """Empty the shared world-dependency pack when another world opens.

    Sparkle has MiEx extract every world's mods and datapacks into the one pack
    mcaddon_world_dependencies, and MiEx never clears it first: a world got the
    models of mods from worlds opened before (an Axiom light-block cube with a
    number on it where Minecraft draws nothing). Returns True when it was emptied.
    """
    folder = Path(resources) / 'mcaddon_world_dependencies'
    try:
        if json.loads((folder / 'mcaddon_world.json').read_text(encoding='utf-8')).get('world') == _world_key(world):
            return False
    except (OSError, ValueError, AttributeError):
        pass
    if not folder.exists():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return True


def mark_world_dependencies(resources, world):
    folder = Path(resources) / 'mcaddon_world_dependencies'
    if folder.is_dir():
        (folder / 'mcaddon_world.json').write_text(json.dumps({'world': _world_key(world)}), encoding='utf-8')


def _references(folder, names):
    """The names (models) that a pack's blockstates or item definitions mention."""
    wanted = {}
    for name in names:
        path = name.split(':', 1)[1] if ':' in name else name
        wanted[name] = ('"' + name + '"', '"' + path + '"') if name.startswith('minecraft:') else ('"' + name + '"',)
    found = set()
    folder = Path(folder)
    roots = [folder] + [p for p in folder.iterdir() if p.is_dir() and (p / 'assets').is_dir()]
    for root in roots:
        for pattern in ('assets/*/blockstates/**/*.json', 'assets/*/items/**/*.json'):
            for path in root.glob(pattern):
                try:
                    text = path.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                for name, needles in wanted.items():
                    if name not in found and any(needle in text for needle in needles):
                        found.add(name)
                if len(found) == len(wanted):
                    return found
    return found


def missing_sources(names, entries):
    """{pack name: [names]} for the packs whose blockstates or item definitions refer
    to the missing models. ``entries`` are (pack name, prepared folder), highest
    priority first; names no pack mentions are under ''."""
    left = list(names)
    out = {}
    for label, folder in entries:
        if not left:
            break
        try:
            hits = _references(folder, left)
        except OSError:
            continue
        if hits:
            out[label] = [name for name in left if name in hits]
            left = [name for name in left if name not in hits]
    if left:
        out[''] = left
    return out


def _few(names, limit=1, short=False):
    shown = [name.rsplit('/', 1)[-1] if short else name[10:] if name.startswith('minecraft:') else name
             for name in names[:limit]]
    return ', '.join(shown) + (', …' if len(names) > limit else '')


def problem_summary(problems, sources=None, messages=()):
    """One short warning (it shares three lines above Import) for what MiEx could
    not find or draw; the log has the full names."""
    parts = []
    models = problems.get('models') or []
    for label, names in (sources or {'': models} if models else {}).items():
        if names:
            parts.append('%d %s missing%s (%s)' % (len(names), 'model' if len(names) == 1 else 'models',
                                                 ' from ' + label if label else '', _few(names, short=True)))
    # Bedrock item frames are exported as entities; MiEx only fails to clear their
    # block above the lowest subchunks (docs/research/miex-upstream-issues.md #2).
    names = [n for n in problems.get('blockstates') or [] if n not in ('minecraft:frame', 'minecraft:glow_frame')]
    if names:
        parts.append('%d %s no blockstate (%s)' % (len(names), 'block has' if len(names) == 1 else 'blocks have', _few(names)))
    names = problems.get('biomes') or []
    if names:
        parts.append('%d unknown %s drawn as plains (%s)' % (len(names), 'biome' if len(names) == 1 else 'biomes', _few(names)))
    failed = problems.get('failed') or []
    if failed:
        parts.append('%d map %s could not be drawn' % (len(failed), 'chunk' if len(failed) == 1 else 'chunks'))
    for message in messages:
        if message and len(parts) < 4:
            parts.append(message)
    return '; '.join(parts)


# MiEx's own [MSG] lines that the problems above already cover, or that say nothing.
NOISE_MESSAGES = ('An error happened somewhere', 'No model file for', 'No blockstate file for',
                  'Resource Pack installed successfully', 'World exported, but some things may be missing')


def useful_messages(lines):
    """MiEx '[MSG] Error/Warning:' texts worth showing, without duplicates."""
    out = []
    for line in lines:
        text = line.split(':', 1)[1].strip() if line.startswith('[MSG]') and ':' in line else line.strip()
        if text and not any(noise in text for noise in NOISE_MESSAGES) and text not in out:
            out.append(text)
    return out
