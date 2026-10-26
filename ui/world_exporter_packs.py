"""The World Exporter shares the scene's resource pack stack with the rest of Sparkle."""


def shared_stack(scene):
    return [(p.name,p.source_path or p.path) for p in scene.mcbedrock_active_packs]


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
    """Add generated/export-preset sources through the same library and activation path."""
    from pathlib import Path
    from . import pack_manager as packs
    import bpy
    scene=context.scene
    path=str(Path(path).resolve())
    index=next((i for i,p in enumerate(scene.mcbedrock_discovered_packs)
                if packs._normalized_path(p.path)==packs._normalized_path(path)),None)
    if index is None:
        if not Path(path).exists():raise ValueError('Resource pack is missing: '+path)
        meta=dict(name=name or Path(path).stem,path=path,launcher='Manual',
                  is_archive=Path(path).is_file(),kind='Java',icon_path='',uuid='')
        packs._remember_manual_pack(meta)
        packs._add_meta_to_collection(scene.mcbedrock_manual_packs,meta)
        packs._add_meta_to_collection(scene.mcbedrock_discovered_packs,meta)
        index=len(scene.mcbedrock_discovered_packs)-1
    result=bpy.ops.mcbedrock.add_resource_pack_to_active(pack_index=index)
    if result!={'FINISHED'}:raise ValueError('Could not activate resource pack: '+path)
    sync_mirror(scene)


def restore_stack(context,rows):
    """Resolve a saved stack fully before replacing the shared active selection."""
    from pathlib import Path
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
