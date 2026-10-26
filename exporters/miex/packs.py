"""Prepare MiEx folder packs without touching the source library."""
import hashlib
import json
import os
from pathlib import Path
import shutil
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


def prepare_packs(entries, resources, cache, cancel):
    from ...importers.archive_inputs import extract_archive_result
    resources = Path(resources)
    resources.mkdir(parents=True, exist_ok=True)
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
                def copy(src, dst):
                    if cancel.is_set():
                        raise MiExError('Cancelled')
                    return shutil.copy2(src, dst)
                shutil.copytree(root, staging, dirs_exist_ok=True, copy_function=copy)
                for attempt in range(4):
                    try:
                        os.replace(staging, destination)
                        break
                    except PermissionError:
                        if attempt == 3:raise
                        if cancel.wait(.1 * (attempt+1)):raise MiExError('Cancelled')
            prepared.append(str(destination.resolve()))
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
