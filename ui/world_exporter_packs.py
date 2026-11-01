"""The World Exporter shares the scene's resource pack stack with the rest of Sparkle."""
import os
from pathlib import Path

# (kind, path) -> 'Java', 'Bedrock' or '': the sidebar asks for every pack each frame.
_EDITIONS={}


def shared_stack(scene):
    return [(p.name,p.source_path or p.path) for p in scene.mcbedrock_active_packs]


def edition(kind,path=''):
    """'Java' or 'Bedrock' from a pack folder's pack.mcmeta or manifest.json,
    else from its RP Manager kind; '' when neither says."""
    key=(kind,path)
    if key not in _EDITIONS:
        java,bedrock=(bool(path) and os.path.isfile(os.path.join(path,name)) for name in ('pack.mcmeta','manifest.json'))
        if java!=bedrock:found='Java' if java else 'Bedrock'
        else:
            from .pack_manager import _edition_label
            label=_edition_label(kind)
            found='Java' if label in {'Java','Mod'} else 'Bedrock' if label.startswith('Bedrock') else ''
        _EDITIONS[key]=found
    return _EDITIONS[key]


def source_kind(path):
    """RP Manager kind of a pack the library has not listed yet."""
    from . import pack_manager as packs
    path=Path(path)
    if path.is_file():return (packs._read_archive_metadata(str(path)) or {}).get('kind') or packs._archive_kind_label(str(path))
    return 'bedrock' if edition('',str(path))=='Bedrock' else 'Java'


def _named(packs,which):
    names=[p.name or Path(p.path).name for p in packs if edition(p.kind,p.path)==which]
    return f'"{names[0]}"' if len(names)==1 else f'the {len(names)} {which} packs' if names else ''


def mixed(scene):
    """Why the active packs can't go to MiEx together, or ''."""
    active=getattr(scene,'mcbedrock_active_packs',())
    java,bedrock=_named(active,'Java'),_named(active,'Bedrock')
    if not (java and bedrock):return ''
    return f"You can't use Java and Bedrock packs together. Turn off {bedrock} or {java}."


def refusal(scene,item):
    """Why switching this library pack on would mix Java and Bedrock packs, or ''."""
    found=edition(item.kind,item.path)
    other={'Java':'Bedrock','Bedrock':'Java'}.get(found)
    clashing=_named(scene.mcbedrock_active_packs,other) if other else ''
    if not clashing:return ''
    return f"You can't use a {found} pack with a {other} pack. Turn off {clashing} first."


def sync_mirror(scene):
    """Keep legacy saved settings readable; the shared scene stack is authoritative."""
    settings=scene.mcbedrock_world_exporter
    rows=shared_stack(scene)
    if rows!=[(p.name,p.path) for p in settings.packs]:
        settings.packs.clear()
        for name,path in rows:
            item=settings.packs.add();item.name=name;item.path=path
    return rows


def add_source(context,path,name=None):
    """Add generated/export-preset sources through the same library and activation path.
    Returns why the pack was left off (it would mix Java and Bedrock packs), or ''."""
    from . import pack_manager as packs
    import bpy
    scene=context.scene
    path=str(Path(path).resolve())
    index=next((i for i,p in enumerate(scene.mcbedrock_discovered_packs)
                if packs._normalized_path(p.path)==packs._normalized_path(path)),None)
    if index is None:
        if not Path(path).exists():raise ValueError('Resource pack is missing: '+path)
        meta=dict(name=name or Path(path).stem,path=path,launcher='Manual',
                  is_archive=Path(path).is_file(),kind=source_kind(path),icon_path='',uuid='')
        packs._remember_manual_pack(meta)
        packs._add_meta_to_collection(scene.mcbedrock_manual_packs,meta)
        packs._add_meta_to_collection(scene.mcbedrock_discovered_packs,meta)
        index=len(scene.mcbedrock_discovered_packs)-1
    item=scene.mcbedrock_discovered_packs[index]
    why='' if packs._active_index_for_source(scene,item.path)>=0 else refusal(scene,item)
    if why:return why
    result=bpy.ops.mcbedrock.add_resource_pack_to_active(pack_index=index)
    if result!={'FINISHED'}:raise ValueError('Could not activate resource pack: '+path)
    sync_mirror(scene)
    return ''


def restore_stack(context,rows):
    """Resolve a saved stack fully before replacing the shared active selection."""
    from types import SimpleNamespace
    from . import pack_manager as packs
    scene=context.scene;prepared=[];missing=[];seen=set()
    for row in rows:
        path=str(Path(row['path']).resolve());key=packs._normalized_path(path)
        if key in seen:continue
        seen.add(key)
        item=next((p for p in scene.mcbedrock_discovered_packs if packs._normalized_path(p.path)==key),None)
        meta=dict(name=row.get('name') or Path(path).stem,path=path,launcher='Manual',
                  is_archive=Path(path).is_file(),kind='Java',icon_path='',uuid='')
        try:
            if not Path(path).exists() or packs._unsupported_archive_reason(path):raise OSError(path)
            if item is None:meta['kind']=source_kind(path)
            root=packs._resolve_pack_root_for_item(item or SimpleNamespace(**meta))
            if not root or not Path(root).is_dir():raise OSError(path)
        except OSError:
            missing.append(meta['name']);continue
        prepared.append((item,meta,root))
    if missing:return missing
    resolved=[]
    for item,meta,root in prepared:
        if item is None:
            packs._remember_manual_pack(meta)
            packs._add_meta_to_collection(scene.mcbedrock_manual_packs,meta)
            packs._add_meta_to_collection(scene.mcbedrock_discovered_packs,meta)
            item=scene.mcbedrock_discovered_packs[-1]
        resolved.append((item,root))
    scene.mcbedrock_active_packs.clear()
    for item,root in resolved:packs._append_active_pack(scene,item,root)
    scene.mcbedrock_active_pack_index=0
    packs.sync_legacy_pack_paths(context,refresh_browser=False,defer_browser=True)
    sync_mirror(scene)
    return []
