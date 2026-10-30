"""Functional native World Exporter UI. All Blender mutations stay on the UI thread."""
import os
from pathlib import Path
import queue
import threading
import time
import uuid
import json
import bpy
from bpy.app.handlers import persistent
from ..exporters.miex.service import Session, MiExError, bundled_runtime, validate_region
from ..exporters.miex.packs import prepare_packs, prepare_template, prepare_biome_defaults, prepare_liquid_models, resolve_ids

_session = None
_session_key = None
_job = None
_generation = 0
_storage = None


def storage():
    # Resolved once per override: the sidebar asks for it while drawing, and every
    # stat or resolve there stalls a frame when the drive is slow or asleep.
    global _storage
    override = os.environ.get('MCADDON_MIEX_HOME')
    if _storage is None or _storage[0] != override:
        _storage = (override, Path(override or bpy.utils.user_resource('DATAFILES', path='mcaddon_miex', create=True)).resolve())
    return _storage[1]


# Map tiles and 3D preview meshes load in the background; they never lock the controls.
BACKGROUND = {'MAP', 'PREVIEW'}


def busy():
    return _job is not None


def foreground_busy():
    return _job is not None and _job.get('action') not in BACKGROUND


def _world_selected(self, context):
    if 0 <= self.world_index < len(self.worlds):
        self.world_path = self.worlds[self.world_index].path
        self.world_name = self.worlds[self.world_index].name
        self.dimensions.clear()
        self.players.clear()


class MCBEDROCK_PG_world_entry(bpy.types.PropertyGroup):
    path: bpy.props.StringProperty()
    launcher: bpy.props.StringProperty()
    display_name: bpy.props.StringProperty()
    edition: bpy.props.StringProperty()
    version: bpy.props.StringProperty()
    icon_path: bpy.props.StringProperty()
    instance: bpy.props.StringProperty()
    x: bpy.props.FloatProperty()
    y: bpy.props.FloatProperty()
    z: bpy.props.FloatProperty()
    dimension: bpy.props.StringProperty()


class MCBEDROCK_PG_world_pack(bpy.types.PropertyGroup):
    path: bpy.props.StringProperty()


def _tell(settings, text, warning=False):
    """Status text the World Exporter also shows above Import (only results show there otherwise)."""
    from . import world_exporter_canvas as canvas
    settings.status = text
    canvas.tell(canvas.state(settings.id_data), text, warning)


def _whitewater_style_changed(self, context):
    """The style acts on the imported world now, not only on the next import."""
    from ..builders import water
    from .world_exporter_layout import imported_world
    scene = context.scene if context else None
    collection = imported_world(scene) if scene is not None else None
    if collection is None or collection.get(water.TAG) not in ('DUNGEONS', 'VIBRANT'):
        return
    try:
        result = water.set_whitewater_style(scene, collection, self.whitewater_style)
    except Exception as exc:
        _tell(self, 'Whitewater needs attention: ' + str(exc), True)
        return
    if result and result.get('supported') is False:
        _tell(self, 'Whitewater around placed objects needs Blender 5.1 or newer', True)
    else:
        _tell(self, collection.name + ': ' + ('simple' if self.whitewater_style == 'SIMPLE' else 'matching') + ' whitewater')



def _plain_waves_changed(self, context):
    from ..builders import water
    from .world_exporter_layout import imported_world
    scene = context.scene if context else None
    collection = imported_world(scene) if scene is not None else None
    if collection is None or collection.get(water.TAG) != 'PLAIN':
        return
    water.set_waves(scene, collection, self.plain_water_waves)
    _tell(self, collection.name + (': waves on' if self.plain_water_waves else ': waves off'))


def _plain_water_changed(self, context):
    from ..builders import water
    water.set_plain_colors(self.id_data)

class MCBEDROCK_PG_world_exporter(bpy.types.PropertyGroup):
    jar: bpy.props.StringProperty(name='MiEx.jar', subtype='FILE_PATH')
    java: bpy.props.StringProperty(name='Java override', default='')
    memory: bpy.props.IntProperty(name='Memory (GB)', default=4, min=1, max=64)
    world_path: bpy.props.StringProperty(name='World folder', subtype='DIR_PATH')
    world_name: bpy.props.StringProperty()
    worlds: bpy.props.CollectionProperty(type=MCBEDROCK_PG_world_entry)
    world_index: bpy.props.IntProperty(default=-1, update=_world_selected)
    dimensions: bpy.props.CollectionProperty(type=MCBEDROCK_PG_world_entry)
    dimension: bpy.props.StringProperty(name='Dimension', default='overworld')
    players: bpy.props.CollectionProperty(type=MCBEDROCK_PG_world_entry)
    player: bpy.props.StringProperty(name='Player')
    packs: bpy.props.CollectionProperty(type=MCBEDROCK_PG_world_pack)
    pack_index: bpy.props.IntProperty()
    available_index: bpy.props.IntProperty()
    min_x: bpy.props.IntProperty(name='Min X', default=0)
    min_y: bpy.props.IntProperty(name='Min Y', default=-64)
    min_z: bpy.props.IntProperty(name='Min Z', default=0)
    max_x: bpy.props.IntProperty(name='Max X', default=15)
    max_y: bpy.props.IntProperty(name='Max Y', default=319)
    max_z: bpy.props.IntProperty(name='Max Z', default=15)
    center: bpy.props.BoolProperty(name='Center at Blender origin', default=True)
    offset_x: bpy.props.IntProperty(default=0)
    offset_y: bpy.props.IntProperty(default=0)
    offset_z: bpy.props.IntProperty(default=0)
    run_optimiser: bpy.props.BoolProperty(default=True)
    fill_caves: bpy.props.BoolProperty(default=False)
    individual: bpy.props.BoolProperty(default=False)
    chunk_size: bpy.props.IntProperty(default=16, min=1, max=256)
    enable_lod: bpy.props.BoolProperty(default=False)
    lod_width: bpy.props.IntProperty(default=256, min=16)
    lod_depth: bpy.props.IntProperty(default=256, min=16)
    lod_detail: bpy.props.IntProperty(default=4, min=1, max=16)
    lod_level: bpy.props.IntProperty(default=0, min=0, max=8)
    excluded: bpy.props.StringProperty(default='[]')
    extra_exclusions: bpy.props.StringProperty(default='[]')
    regions: bpy.props.StringProperty(default='[]')
    region_name: bpy.props.StringProperty(default='Selection 1')
    foreground: bpy.props.StringProperty(default='[]')
    entity_start: bpy.props.IntProperty(default=1)
    entity_end: bpy.props.IntProperty(default=100)
    entity_fps: bpy.props.IntProperty(default=24, min=1, max=240)
    entity_seed: bpy.props.IntProperty(default=0)
    entity_density: bpy.props.IntProperty(default=100, min=0)
    entity_light: bpy.props.IntProperty(default=15, min=0, max=15)
    entity_types: bpy.props.StringProperty(default='')
    entity_simulate: bpy.props.StringProperty(default='')
    entity_spawn: bpy.props.StringProperty(default='')
    auto_import: bpy.props.BoolProperty(default=True)
    reduce_motion: bpy.props.BoolProperty(name='Reduce interface motion',default=False)
    preset_name: bpy.props.StringProperty(default='My selection')
    atlas_repeats: bpy.props.IntProperty(default=4, min=1, max=32)
    atlas_padding: bpy.props.IntProperty(default=1, min=0, max=16)
    tool_pack_name: bpy.props.StringProperty(default='Sparkle_Generated')
    modpack_path: bpy.props.StringProperty(subtype='DIR_PATH')
    remove_caves: bpy.props.BoolProperty(name='Remove caves', default=False)
    block_animations: bpy.props.BoolProperty(name='Animated blocks', default=False)
    entities: bpy.props.BoolProperty(name='Entities', default=False)
    world_packs: bpy.props.BoolProperty(name='Include world resource packs', default=True,
        description='Let MiEx resolve world dependencies below your chosen packs')
    engine: bpy.props.EnumProperty(name='Materials', items=[('CYCLES', 'Cycles', ''), ('EEVEE', 'EEVEE', '')])
    water_preset: bpy.props.EnumProperty(name='Water style', default='ORIGINAL', items=[
        ('ORIGINAL', 'Original', 'Keep the resource pack water; restore it on an existing import'),
        ('DUNGEONS', 'MC Dungeons II', 'Flat deep-blue water with subtle reflections and a clean white contact outline'),
        ('VIBRANT', 'Vibrant Visuals', 'Clear water with native animated pixel normals and sun reflections'),
        ('PLAIN', 'Plain', 'Flat stylized blue with clean white water along every shore, like key-art renders')])
    whitewater_style: bpy.props.EnumProperty(name='Whitewater style', default='MATCH', update=_whitewater_style_changed, items=(
        ('MATCH', 'Match water', 'Pixel foam for MC Dungeons II, soft froth for Vibrant Visuals'),
        ('SIMPLE', 'Simple', 'One plain white outline around anything in the water')))
    outlines: bpy.props.BoolProperty(name='Outlines', default=False,
        description='Add Sparkle outlines to the imported blocks: soft block edges that follow the lights, not drawn lines. Off unless you turn it on')
    plain_water_color: bpy.props.FloatVectorProperty(name='Water color', subtype='COLOR', size=3, min=0, max=1,
        default=(.017, .3, .83), update=_plain_water_changed,
        description='The color of Plain water. With Biome colors on, each biome shifts it the way normal water is recolored')
    plain_water_waves: bpy.props.BoolProperty(name='Waves', default=True, update=lambda self, context: _plain_waves_changed(self, context),
        description='The water and its white shore line rise and fall gently, like Dungeons II')
    plain_water_biome: bpy.props.BoolProperty(name='Biome colors', default=True, update=_plain_water_changed,
        description='Recolor Plain water per biome, like normal Minecraft water: warm oceans turquoise, swamps murky')
    output: bpy.props.StringProperty(name='Output folder', subtype='DIR_PATH')
    status: bpy.props.StringProperty(default='Choose a world to begin', options={'SKIP_SAVE'})
    last_export: bpy.props.StringProperty(subtype='FILE_PATH')
    log_path: bpy.props.StringProperty(subtype='FILE_PATH')


class MCBEDROCK_UL_worlds(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon='WORLD')


class MCBEDROCK_UL_world_packs(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon='PACKAGE' if os.path.exists(item.path) else 'ERROR')


def _snapshot(context):
    settings = context.scene.mcbedrock_world_exporter
    home = storage()
    bundled_jar, bundled_java = bundled_runtime()
    jar = bpy.path.abspath(settings.jar) if settings.jar else bundled_jar
    output = bpy.path.abspath(settings.output) if settings.output else (
        str(Path(bpy.data.filepath).parent / 'MiEx Exports') if bpy.data.filepath else str(home / 'exports'))
    region = {side + axis.upper(): getattr(settings, side + '_' + axis)
              for side in ('min', 'max') for axis in 'xyz'}
    for axis in 'XYZ':
        region['offset' + axis] = ((region['min' + axis] + region['max' + axis]) // 2
                                  if settings.center and axis != 'Y' else getattr(settings, 'offset_' + axis.lower()))
    from ..exporters.miex.map_model import exclusions
    region.update(name=settings.region_name,enableLOD=settings.enable_lod, lodCenterX=(settings.min_x+settings.max_x)//2,
        lodCenterZ=(settings.min_z+settings.max_z)//2, lodWidth=settings.lod_width,
        lodDepth=settings.lod_depth, lodYDetail=settings.lod_detail, lodBaseLevel=settings.lod_level,
        onlyIndividualBlocks=settings.individual, chunkSize=settings.chunk_size,
        excludeRegions=exclusions(json.loads(settings.excluded),settings.min_y,settings.max_y)+json.loads(settings.extra_exclusions),
        fgChunks=json.loads(settings.foreground))
    extra_regions=json.loads(settings.regions)
    if settings.center:
        # Separate regions must retain their relative positions after import.
        for axis in 'XZ':
            regions=[region]+extra_regions
            origin=(min(r['min'+axis] for r in regions)+max(r['max'+axis] for r in regions))//2
            for r in regions:r['offset'+axis]=origin
    return dict(home=str(home), jar=jar, java=settings.java or bundled_java, memory=settings.memory,
        world=bpy.path.abspath(settings.world_path) if settings.world_path else '',
        dimension=settings.dimension, engine=settings.engine, region=region,
        removeCaves=settings.remove_caves, blockAnimations=settings.block_animations,
        entities=settings.entities, world_packs=settings.world_packs, output=output,
        auto_import=settings.auto_import,
        extra_regions=extra_regions,
        options=dict(chunkSize=settings.chunk_size,runOptimiser=settings.run_optimiser,fillInCaves=settings.fill_caves,
            entityStartFrame=settings.entity_start,entityEndFrame=settings.entity_end,entityFPS=settings.entity_fps,
            entityRandomSeed=settings.entity_seed,entitySpawnDensity=settings.entity_density,entitySunLightLevel=settings.entity_light,
            entitySimulate=[s.strip() for s in settings.entity_simulate.split(',') if s.strip()],
            entitySpawnRules=[s.strip() for s in settings.entity_spawn.split(',') if s.strip()]),
        entity_types=[s.strip() for s in settings.entity_types.split(',') if s.strip()],
        tool_pack=settings.tool_pack_name,atlas_repeats=settings.atlas_repeats,atlas_padding=settings.atlas_padding,
        modpack_path=bpy.path.abspath(settings.modpack_path),
        packs=[dict(name=p.name, path=bpy.path.abspath(p.source_path or p.path))
               for p in context.scene.mcbedrock_active_packs])


def _connect(snapshot, job):
    global _session, _session_key
    if job['cancel'].is_set():
        raise MiExError('Cancelled')
    key = (snapshot['jar'], snapshot['java'], snapshot['memory'], snapshot['home'])
    if _session is not None and (_session_key != key or _session.cancelled.is_set() or
                                 _session.process is None or _session.process.poll() is not None):
        _session.close()
        _session = None
    if _session is None:
        _session = Session(snapshot['java'], snapshot['jar'], Path(snapshot['home']) / 'session', snapshot['memory'])
        _session_key = key
        job['session'] = _session
        if job['cancel'].is_set():
            _session.close()
            raise MiExError('Cancelled')
        job['events'].put(('status', 'Starting MiEx (first setup can take a few minutes)…'))
        _session.start()
    job['session'] = _session
    return _session


def _prepare_world(snapshot, job, force=False):
    resources = Path(snapshot['home']) / 'session' / 'resources'
    session = _connect(snapshot, job)
    key = (snapshot['world'], snapshot['dimension'], snapshot['world_packs'], snapshot['engine'],
           tuple((p['name'], p['path']) for p in snapshot['packs']))
    if force or getattr(session, 'world_key', None) != key:
        if not Path(snapshot['world']).is_dir():
            raise MiExError('Choose an existing Minecraft world folder')
        template = prepare_template(resources, snapshot['engine'])
        biome_defaults = prepare_biome_defaults(resources)
        job['events'].put(('status', 'Loading world and resource packs…'))
        session.request('loadWorld', world=snapshot['world'], loadWorldResourcePacks=snapshot['world_packs'],
                        worldResourcePackName='mcaddon_world_dependencies')
        world = session.request('getWorld').get('world', {})
        if not world.get('path'):
            raise MiExError('MiEx could not load this world; see log')
        warning = ''
        if world.get('version',0)>0:
            job['events'].put(('status', 'Preparing Minecraft assets for this world…'))
            from ..exporters.miex.worlds import client_folders
            warning = session.request('mcaddonBase',worldVersion=world['version'],clientFolders=client_folders(),
                                      timeout=600).get('warning', '')
        dimension = snapshot['dimension']
        if dimension not in world.get('dimensions', []):
            dimension = world.get('dimension', 'overworld')
        session.request('loadDimension', dimension=dimension)
        session.defaults = session.request('getExportSettings').get('settings', {})
        prepared = prepare_packs(snapshot['packs'], resources, Path(snapshot['home']) / 'archive_cache', job['cancel'])
        dependencies = session.request('getActiveResourcePacks').get('resourcePacks', [])
        prepare_liquid_models(resources)
        session.request('reloadResourcePacks')
        rows = session.request('mcaddonPacks')['resourcePacks']
        chosen = resolve_ids([template] + prepared, rows)
        if snapshot['world_packs']:
            chosen += [r['uuid'] for r in rows if Path(r['folder']).name == 'mcaddon_world_dependencies'
                       and r['uuid'] in dependencies and r['uuid'] not in chosen]
        chosen += resolve_ids([biome_defaults], rows)
        try:
            session.request('setActiveResourcePacks', resourcePacks=chosen)
        except MiExError as exc:
            names = ', '.join(p['name'] for p in snapshot['packs']) or 'world packs'
            raise MiExError('%s Packs being loaded: %s.' % (exc, names)) from exc
        if session.request('getActiveResourcePacks')['resourcePacks'] != chosen + ['base_resource_pack']:
            raise MiExError('MiEx pack order did not match the selected order')
        session.world_key = key
        session.world_info = session.request('getWorld')['world']
        if warning:
            session.world_info['assets_warning'] = warning
    return session


def _run(job, action, snapshot):
    try:
        events = job['events']
        if job['cancel'].is_set():
            raise MiExError('Cancelled')
        if action in {'CHECK', 'WORLDS', 'BASE'}:
            session = _connect(snapshot, job)
            if action == 'CHECK':
                session.request('mcaddonPacks')
                result = 'MiEx connected and ready'
            elif action == 'WORLDS':
                result = session.discover_worlds()
            else:
                from ..exporters.miex.worlds import client_folders
                session.request('mcaddonBase',clientFolders=client_folders(),timeout=600)
                session.world_key=None
                result='Vanilla base pack updated from installed Minecraft'
        else:
            session = _prepare_world(snapshot, job, force=action == 'LOAD')
            if action == 'LOAD':
                result = session.world_info
            elif action == 'MAP':
                from ..exporters.miex.map_model import decode_chunk
                rows = session.request('mcaddonMap', timeout=120, chunks=job['chunks'])['chunks']
                # Decoded here rather than on Blender's UI thread when the batch lands.
                result = [dict(chunkX=row['chunkX'], chunkZ=row['chunkZ'], tile=decode_chunk(row)) for row in rows]
            elif action == 'PREVIEW':
                from ..exporters.miex.preview import COMMAND, decode_chunks
                rows = session.request(COMMAND, timeout=300, chunks=job['chunks']).get('chunks', [])
                result = decode_chunks(rows)
            elif action in {'ATLAS', 'PBR', 'MODPACK'}:
                name = snapshot['tool_pack']
                if not name or name in {'.', '..'} or any(c in name for c in '/\\:'):
                    raise MiExError('Generated pack name must be a simple folder name')
                ids = session.request('getActiveResourcePacks')['resourcePacks']
                if action == 'ATLAS':
                    session.request('mcaddonAtlas', timeout=7200,resourcePacks=ids,saveToResourcePack=name,
                                    repeats=snapshot['atlas_repeats'],padding=snapshot['atlas_padding'])
                elif action == 'PBR':
                    session.request('mcaddonPbr', timeout=7200,resourcePacks=ids,saveToResourcePack=name)
                else:
                    if not Path(snapshot['modpack_path']).is_dir():
                        raise MiExError('Choose the modpack mods folder')
                    session.request('extractModResourcePack',timeout=7200,modpack=snapshot['modpack_path'],resourcePack=name)
                session.world_key = None
                result = str(Path(snapshot['home']) / 'session' / 'resources' / name)
                if not any(p.is_file() for p in Path(result).rglob('*')):
                    raise MiExError('MiEx generated no files. Check the selected packs and tool configuration in the log.')
            else:
                if snapshot['dimension'] not in session.world_info.get('dimensions', []):
                    raise MiExError('Choose one of this world’s dimensions')
                options = dict(snapshot['options'])
                options['entityExport'] = (snapshot['entity_types'] or session.defaults.get('entityExport', [])) if snapshot['entities'] else []
                if snapshot['entities'] and not options['entityExport']:
                    raise MiExError('Entity export is enabled, but no Entity IDs are selected. Add IDs or turn it off.')
                configured = session.request('mcaddonConfigure', region=snapshot['region'], options=options,
                    extraRegions=snapshot['extra_regions'],
                    removeCaves=snapshot['removeCaves'], blockAnimations=snapshot['blockAnimations'],
                    entities=snapshot['entities'])
                if any(configured['region'].get(k) != v for k, v in snapshot['region'].items()):
                    raise MiExError('MiEx did not apply the requested region')
                if len(configured.get('regions', [])) != len(snapshot['extra_regions'])+1 or any(
                        any(actual.get(key) != value for key, value in requested.items())
                        for actual, requested in zip(configured['regions'][1:], snapshot['extra_regions'])):
                    raise MiExError('MiEx did not apply the saved regions')
                requested_settings=dict(options,removeCaves=snapshot['removeCaves'],
                    blockAnimations=snapshot['blockAnimations'])
                effective=configured.get('settings', {})
                for key,value in requested_settings.items():
                    if effective.get(key)!=value:
                        raise MiExError('MiEx did not apply '+key+'; export stopped')
                target_dir = Path(snapshot['output']) / ('world_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
                target_dir.mkdir(parents=True, exist_ok=False)
                path = target_dir / 'world.usd'
                events.put(('status', 'Exporting selected region…'))
                session.request('export', timeout=7200, path=str(path))
                if not path.is_file() or not path.with_name('world_materials.json').is_file():
                    raise MiExError('MiEx did not produce a complete export; see log')
                from ..exporters.miex.portable import preserve_textures
                preserve_textures(path, job['cancel'])
                (target_dir / 'mcaddon_selection.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
                result = str(path)
        if job['cancel'].is_set():
            raise MiExError('Cancelled')
        job['events'].put(('done', (action, result)))
    except Exception as exc:
        session = job.get('session')
        if session:
            session.close()
        job['events'].put(('error', str(exc)))


def _poll():
    global _job
    job = _job
    if job is None:
        return None
    try:
        scene = next((s for s in bpy.data.scenes if s.session_uid == job['scene']), None)
        if scene is None or job['generation'] != _generation:
            _stop()
            return None
        settings = scene.mcbedrock_world_exporter
        arrived = 0
        while True:
            try:
                kind, result = job['events'].get_nowait()
            except queue.Empty:
                break
            arrived += 1
            if kind == 'status':
                settings.status = result
            elif kind == 'error':
                settings.status = ('Cancelled' if job['cancel'].is_set() else 'Failed: ' + result)
                from . import world_exporter_canvas
                world_exporter_canvas.job_error(scene, job)
                _job = None
            elif kind == 'done':
                _job = None
                action, value = result
                if job['cancel'].is_set():
                    settings.status = 'Cancelled'
                    continue
                if action in {'CHECK','BASE'}:
                    settings.status = value
                    if action=='BASE':
                        from . import world_exporter_canvas
                        world_exporter_canvas.invalidate(scene)
                elif action == 'WORLDS':
                    settings.world_index = -1
                    settings.worlds.clear()
                    for row in value:
                        entry = settings.worlds.add()
                        entry.name, entry.path = row['name'], row['path']
                        entry.launcher = row['launcher']
                        for key in ('display_name', 'edition', 'version', 'icon_path', 'instance'):
                            setattr(entry, key, row.get(key, ''))
                    settings.status = '%d worlds found; select one or browse to its folder' % len(value)
                    from . import world_exporter_canvas
                    world_exporter_canvas.state(scene).worlds_refresh_started=time.monotonic()
                elif action == 'LOAD':
                    match=next((w for w in settings.worlds if w.path==settings.world_path),None)
                    settings.world_name = match.name if match else value.get('name') or Path(settings.world_path).name
                    settings.dimensions.clear()
                    for name in value.get('dimensions', []):
                        settings.dimensions.add().name = name
                    settings.dimension = value.get('dimension', 'overworld')
                    settings.players.clear()
                    for row in value.get('players', []):
                        from ..exporters.miex.worlds import dimension_key
                        player = settings.players.add()
                        player.name = row.get('name') or row.get('uuid', 'Player')
                        player.dimension = next((d.name for d in settings.dimensions if dimension_key(d.name)==dimension_key(row.get('dimension',''))),row.get('dimension',''))
                        for axis in 'xyz':
                            setattr(player, axis, row.get(axis, 0))
                    settings.status = value.get('assets_warning') or 'World loaded. Drag on the map to select an area.'
                    from . import world_exporter_canvas
                    world_exporter_canvas.world_loaded(scene, value)
                elif action == 'MAP':
                    from . import world_exporter_canvas
                    world_exporter_canvas.receive_tiles(scene, job, value)
                elif action == 'PREVIEW':
                    from . import world_exporter_canvas
                    world_exporter_canvas.receive_preview(scene, job, value)
                elif action in {'ATLAS', 'PBR', 'MODPACK'}:
                    if Path(value).is_dir():
                        from .world_exporter_packs import add_source
                        with bpy.context.temp_override(scene=scene):
                            add_source(bpy.context,value)
                    settings.status = 'Generated pack: ' + Path(value).name
                    from . import world_exporter_canvas
                    world_exporter_canvas.invalidate(scene)
                elif action == 'EXPORT':
                    settings.last_export = value
                    if not job['snapshot']['auto_import']:
                        settings.status = 'Export saved: ' + value
                        continue
                    settings.status = 'Importing world into Blender…'
                    window = next((w for w in bpy.context.window_manager.windows if w.as_pointer() == job['window']), None)
                    if not window or window.scene != scene:
                        settings.status = 'Export ready. Return to the destination scene and choose Import Last Export.'
                    else:
                        _schedule_import(scene,window)
        # Only news redraws the map; its own timer animates progress.
        for window in bpy.context.window_manager.windows if arrived else ():
            if not window.workspace.get('mcaddon_world_exporter_workspace'):continue
            for area in window.screen.areas:
                if area.type=='IMAGE_EDITOR':area.tag_redraw()
    except Exception as exc:
        if scene:
            scene.mcbedrock_world_exporter.status = 'Failed: ' + str(exc)
        _job = None
    return (.05 if _job.get('action') in BACKGROUND else .1) if _job else None


def _schedule_import(scene,window):
    """Let Blender paint the non-cancellable import stage before blocking on USD."""
    from . import world_exporter_canvas as canvas
    owner=scene.session_uid;window_id=window.as_pointer();generation=_generation
    canvas.state(scene).importing=True
    def finish_import():
        if generation!=_generation:return None
        scene=next((s for s in bpy.data.scenes if s.session_uid==owner),None)
        if scene is None:return None
        try:
            window=next((w for w in bpy.context.window_manager.windows if w.as_pointer()==window_id),None)
            if window is None or window.scene!=scene:
                scene.mcbedrock_world_exporter.status='Export ready. Return to the destination scene to import.'
                return None
            with bpy.context.temp_override(window=window,scene=scene):
                bpy.ops.ed.undo_push(message='Before MiEx Import')
                result=bpy.ops.mcbedrock.world_import('EXEC_DEFAULT')
                if result=={'FINISHED'}:bpy.ops.ed.undo_push(message='Import MiEx World')
        except Exception as exc:
            scene.mcbedrock_world_exporter.status='Import failed: '+str(exc)+'. Export retained for retry.'
        finally:
            canvas.state(scene).importing=False
        return None
    bpy.app.timers.register(finish_import,first_interval=.15)


def _stop():
    global _job, _session, _session_key
    if _job:
        _job['cancel'].set()
        session = _job.get('session')
        if session:
            session.close()
        _job['thread'].join(timeout=3)
    if _session:
        _session.close()
    _session = None
    _session_key = None
    _job = None


OUTLINES_DEFAULT = 'mc_outlines_default_off'   # scene: imports outline nothing until asked


def outlines_off_by_default(scene):
    """Outlines are a per-world choice, off by default. Files saved while the
    import toggle was on (from outline testing) kept outlining every new
    import; switch it off once, after which the toggle is the user's again."""
    settings = getattr(scene, 'mcbedrock_world_exporter', None)
    if settings is None or scene.get(OUTLINES_DEFAULT):
        return
    settings.outlines = False
    scene[OUTLINES_DEFAULT] = True


@persistent
def _after_load(_unused):
    for scene in bpy.data.scenes:
        if not scene.library:
            outlines_off_by_default(scene)


@persistent
def _before_load(_unused):
    global _generation
    _generation += 1
    _stop()
    from . import world_exporter_canvas
    world_exporter_canvas.reset()


def start_job(context, action, stack=None, **extra):
    """``stack``: (name, path) packs to use instead of the scene's, for map and
    3D requests while a changed pack stack waits for Reload."""
    global _job
    if busy():
        return False
    snapshot = _snapshot(context)
    if stack is not None:
        snapshot['packs'] = [dict(name=name, path=bpy.path.abspath(path)) for name, path in stack]
    if action == 'EXPORT':
        validate_region(snapshot['region'])
        for region in snapshot['extra_regions']:
            validate_region(region)
        names=[snapshot['region']['name']]+[r.get('name','') for r in snapshot['extra_regions']]
        if len(names)!=len(set(names)):
            raise MiExError('Give each export region a different name')
    settings = context.scene.mcbedrock_world_exporter
    if action not in BACKGROUND:
        settings.status = 'Preparing…'
    settings.log_path = str(Path(snapshot['home']) / 'session' / 'miex.log')
    _job = dict(events=queue.Queue(), cancel=threading.Event(), scene=context.scene.session_uid,
                window=context.window.as_pointer() if context.window else 0, generation=_generation,
                snapshot=snapshot, action=action, **extra)
    thread = threading.Thread(target=_run, args=(_job, action, snapshot), daemon=True)
    _job['thread'] = thread
    thread.start()
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=.1)
    return True


class MCBEDROCK_OT_world_job(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_job'
    bl_label = 'MiEx Operation'
    action: bpy.props.EnumProperty(items=[(v, v.title(), '') for v in ('CHECK', 'WORLDS', 'LOAD', 'EXPORT', 'ATLAS', 'PBR', 'MODPACK', 'BASE')])

    @classmethod
    def poll(cls, context):
        return context.scene is not None and not busy()

    def execute(self, context):
        try:
            start_job(context, self.action)
        except (MiExError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class MCBEDROCK_OT_world_cancel(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_cancel'
    bl_label = 'Cancel'

    def execute(self, context):
        if _job:
            _job['cancel'].set()
            if _job.get('session'):
                _job['session'].close()
            context.scene.mcbedrock_world_exporter.status = 'Cancelling…'
        return {'FINISHED'}


class MCBEDROCK_OT_world_import(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_import'
    bl_label = 'Import Last Export'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from ..exporters.miex.import_bridge import import_world
        settings = context.scene.mcbedrock_world_exporter
        try:
            if context.mode != 'OBJECT':
                raise MiExError('Switch to Object Mode before importing')
            traced = getattr(getattr(context.scene, 'eevee', None), 'use_raytracing', False)
            collection = import_world(context, settings.last_export, Path(settings.world_path).name or 'World',
                                      water_preset=settings.water_preset, outlines=settings.outlines,
                                      whitewater_style=settings.whitewater_style)
            settings.status = 'Imported into ' + collection.name
            if settings.outlines:
                settings.status += '; ' + _outline_world(context, collection, True)
            if not traced and getattr(getattr(context.scene, 'eevee', None), 'use_raytracing', False):
                settings.status += '; EEVEE ray tracing enabled for water'
            if collection.get('mcaddon_water_foam_truncated'):
                coverage = ('Whitewater coverage limited' if collection.get('mcaddon_water_foam_patches')
                            else 'No whitewater generated')
                reason = collection.get('mcaddon_water_foam_reason', 'Outline limit')
                settings.status += '; ' + coverage.lower() + ': ' + reason.lower()
                self.report({'WARNING'}, coverage + ' (' + reason + ')')
            if collection.get('mcaddon_water_caustics_truncated'):
                coverage = ('Underwater light coverage limited' if collection.get('mcaddon_water_caustics_patches')
                            else 'No underwater light patterns generated')
                reason = collection.get('mcaddon_water_caustics_reason', 'Projection limit')
                settings.status += '; ' + coverage.lower() + ': ' + reason.lower()
                self.report({'WARNING'}, coverage + ' (' + reason + '); try a smaller or simpler area')
        except Exception as exc:
            settings.status = 'Import failed: ' + str(exc) + '. Export retained for retry.'
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


def _outline_world(context, collection, enable):
    """Opt an import in or out of outlines and say what the user will see."""
    from ..builders import outlines
    try:
        return outlines.set_world(context.scene, collection, enable)
    except Exception as exc:
        return 'outlines need attention: ' + str(exc)


def _trailer_world(context, collection, enable):
    """Apply or remove trailer materials on an import and say what changed."""
    from ..builders import trailer_materials
    try:
        return trailer_materials.set_world(context.scene, collection, enable)
    except Exception as exc:
        return 'trailer materials need attention: ' + str(exc)


class MCBEDROCK_OT_plain_water_color(bpy.types.Operator):
    bl_idname = 'mcbedrock.plain_water_color'
    bl_label = 'Plain water color'
    bl_description = 'Choose the color of Plain water'
    bl_options = {'UNDO'}
    color: bpy.props.FloatVectorProperty(subtype='COLOR', size=3, min=0, max=1)

    def invoke(self, context, event):
        self.color = context.scene.mcbedrock_world_exporter.plain_water_color
        return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        self.layout.template_color_picker(self, 'color', value_slider=True)
        self.layout.prop(self, 'color', text='')

    def execute(self, context):
        context.scene.mcbedrock_world_exporter.plain_water_color = self.color
        return {'FINISHED'}


class MCBEDROCK_OT_world_materials(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_materials'
    bl_label = 'Trailer Materials'
    bl_description = 'Apply or remove trailer materials on an imported MiEx world'
    bl_options = {'REGISTER', 'UNDO'}
    collection_name: bpy.props.StringProperty(options={'HIDDEN'})
    enable: bpy.props.BoolProperty(default=True, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.mode == 'OBJECT'

    def execute(self, context):
        from ..exporters.miex.world_scale import imported_collection
        collection = imported_collection(context, self.collection_name)
        if collection is None:
            self.report({'WARNING'}, 'Select an object in a MiEx world first')
            return {'CANCELLED'}
        settings = context.scene.mcbedrock_world_exporter
        settings.status = collection.name + ': ' + _trailer_world(context, collection, self.enable)
        self.report({'INFO'}, settings.status)
        return {'FINISHED'}


class MCBEDROCK_OT_world_outlines(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_outlines'
    bl_label = 'Outline Imported World'
    bl_description = 'Add or remove Sparkle outlines on an imported MiEx world'
    bl_options = {'REGISTER', 'UNDO'}
    collection_name: bpy.props.StringProperty(options={'HIDDEN'})
    enable: bpy.props.BoolProperty(default=True, options={'HIDDEN'})
    every: bpy.props.BoolProperty(default=False, options={'HIDDEN'},
        description='Every imported world in this scene')

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.mode == 'OBJECT'

    def execute(self, context):
        from ..exporters.miex.world_scale import imported_collection
        if self.every:
            targets = [c for c in context.scene.collection.children_recursive if c.get('mcaddon_miex_export')]
        else:
            target = imported_collection(context, self.collection_name)
            targets = [target] if target is not None else []
        if not targets:
            self.report({'WARNING'}, 'Select an object in a MiEx world first')
            return {'CANCELLED'}
        for collection in targets:
            message = _outline_world(context, collection, self.enable)
        name = targets[0].name if len(targets) == 1 else f'{len(targets)} worlds'
        settings = context.scene.mcbedrock_world_exporter
        settings.status = name + ': ' + message
        self.report({'INFO'}, settings.status)
        return {'FINISHED'}


class MCBEDROCK_OT_world_water(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_water'
    bl_label = 'Apply Water Style'
    bl_description = 'Change water in the selected or most recently imported MiEx world'
    bl_options = {'REGISTER', 'UNDO'}
    collection_name: bpy.props.StringProperty(options={'HIDDEN'})
    restore_original: bpy.props.BoolProperty(default=False, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return (context.scene is not None and context.mode == 'OBJECT' and
                not foreground_busy())

    def execute(self, context):
        from ..builders import water
        from ..exporters.miex.world_scale import imported_collection
        collection = imported_collection(context, self.collection_name)
        if collection is None:
            self.report({'WARNING'}, 'Select an object in a MiEx world first')
            return {'CANCELLED'}
        settings = context.scene.mcbedrock_world_exporter
        preset = 'ORIGINAL' if self.restore_original else settings.water_preset
        try:
            result = water.apply(context.scene, collection, preset,
                                 whitewater_style=None if preset == 'ORIGINAL' else settings.whitewater_style)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        message = ('Original water restored' if preset == 'ORIGINAL' else
                   settings.bl_rna.properties['water_preset'].enum_items[preset].name + ' water applied')
        already_applied = preset != 'ORIGINAL' and any(
            slot.material and slot.material.get(water.TAG) == preset
            for obj in collection.all_objects if obj.type == 'MESH' for slot in obj.material_slots)
        if not result['changed'] and not already_applied:
            message = 'No matching water to restore' if preset == 'ORIGINAL' else 'No replaceable water found'
        if result['skipped']:
            message += f"; {result['skipped']} shared or edited slots kept"
        if result.get('raytracing_enabled'):
            message += '; EEVEE ray tracing enabled for water'
        if preset == 'DUNGEONS' and collection.get('mcaddon_water_foam_truncated'):
            coverage = ('whitewater coverage limited' if collection.get('mcaddon_water_foam_patches')
                        else 'no whitewater generated')
            reason = collection.get('mcaddon_water_foam_reason', 'Outline limit')
            message += '; ' + coverage + ' (' + reason.lower() + ')'
        if preset == 'VIBRANT' and collection.get('mcaddon_water_caustics_truncated'):
            coverage = ('underwater light coverage limited' if collection.get('mcaddon_water_caustics_patches')
                        else 'no underwater light patterns generated')
            reason = collection.get('mcaddon_water_caustics_reason', 'Projection limit')
            message += '; ' + coverage + ' (' + reason.lower() + ')'
        settings.status = 'Imported world: ' + message + ' (' + collection.name + ')'
        self.report({'INFO'}, message + ' in ' + collection.name)
        return {'FINISHED'}


class MCBEDROCK_OT_world_frame(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_frame'
    bl_label = 'Frame Imported World'
    bl_description = 'Fit the selected or most recently imported world in the destination 3D view'

    def execute(self, context):
        from ..exporters.miex.world_scale import imported_collection, frame_world
        collection = imported_collection(context)
        if collection is None:
            self.report({'WARNING'}, 'Select an object in the imported world first')
            return {'CANCELLED'}
        if not frame_world(context, collection, show=True):
            self.report({'WARNING'}, 'Open a 3D viewport to frame this world')
            return {'CANCELLED'}
        return {'FINISHED'}


class MCBEDROCK_OT_world_scale_check(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_scale_check'
    bl_label = 'Check World Scale'
    bl_description = 'Compare this imported world with its source USD without changing it'
    collection_name: bpy.props.StringProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        from ..exporters.miex.world_scale import imported_collection, diagnose_scale
        collection = imported_collection(context, self.collection_name)
        if collection is None:
            self.report({'WARNING'}, 'Select an object in the imported world first')
            return {'CANCELLED'}
        self.collection_name = collection.name
        self._result = diagnose_scale(collection)
        return context.window_manager.invoke_props_dialog(self, width=540)

    def draw(self, context):
        layout = self.layout
        result = self._result
        layout.label(text=self.collection_name, icon='OUTLINER_COLLECTION')
        layout.label(text=result['message'])
        if result.get('detail'):
            layout.label(text=result['detail'])
        if result['state'] == 'MISMATCH':
            op = layout.operator('mcbedrock.world_scale_apply', text='Rescale this world in place')
            op.collection_name = self.collection_name
            op.action = 'RESCALE'
        if result['state'] != 'CORRECT':
            op = layout.operator('mcbedrock.world_scale_apply', text='Import corrected copy (keep original)')
            op.collection_name = self.collection_name
            op.action = 'REIMPORT'
        layout.label(text='Existing worlds are never resized automatically.')

    def execute(self, context):
        from ..exporters.miex.world_scale import imported_collection, diagnose_scale
        collection = imported_collection(context, self.collection_name)
        if collection is None:
            self.report({'WARNING'}, 'Select an object in the imported world first')
            return {'CANCELLED'}
        self.report({'INFO'}, diagnose_scale(collection)['message'])
        return {'FINISHED'}


class MCBEDROCK_OT_world_scale_apply(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_scale_apply'
    bl_label = 'Correct World Scale'
    bl_options = {'REGISTER', 'UNDO'}
    collection_name: bpy.props.StringProperty(options={'HIDDEN'})
    action: bpy.props.EnumProperty(items=[('RESCALE', 'Rescale in place', ''),
                                         ('REIMPORT', 'Import corrected copy', '')])

    def execute(self, context):
        from ..exporters.miex.world_scale import imported_collection, rescale_world, frame_world
        from ..exporters.miex.import_bridge import import_world
        collection = imported_collection(context, self.collection_name)
        try:
            if collection is None:
                raise MiExError('The imported world is no longer in this scene')
            if context.mode != 'OBJECT':
                raise MiExError('Switch to Object Mode before correcting scale')
            if self.action == 'REIMPORT':
                collection = import_world(context, collection['mcaddon_miex_export'],
                                          collection.name + ' corrected',
                                          outlines=bool(collection.get('mc_world_outlines')))
            else:
                rescale_world(context, collection)
                frame_world(context, collection)
            context.scene.mcbedrock_world_exporter.status = 'Imported world scale: one block per Blender unit'
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class MCBEDROCK_OT_world_pack(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_pack'
    bl_label = 'Change Export Resource Packs'
    action: bpy.props.EnumProperty(items=[(v, v.title(), '') for v in ('ADD', 'REMOVE', 'UP', 'DOWN', 'COPY')])

    def execute(self, context):
        settings = context.scene.mcbedrock_world_exporter
        if foreground_busy():
            return {'CANCELLED'}
        scene=context.scene
        result={'FINISHED'}
        if self.action=='ADD':
            result=bpy.ops.mcbedrock.add_resource_pack_to_active(pack_index=settings.available_index)
        elif self.action!='COPY' and 0<=settings.pack_index<len(scene.mcbedrock_active_packs):
            scene.mcbedrock_active_pack_index=settings.pack_index
            operators={'REMOVE':'remove_active_pack','UP':'move_active_pack_up','DOWN':'move_active_pack_down'}
            result=getattr(bpy.ops.mcbedrock,operators[self.action])()
        from .world_exporter_packs import sync_mirror
        sync_mirror(scene)
        settings.pack_index=scene.mcbedrock_active_pack_index
        return result


class MCBEDROCK_OT_world_center(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_center'
    bl_label = 'Center Selection on Player'

    def execute(self, context):
        import math
        settings = context.scene.mcbedrock_world_exporter
        player = settings.players.get(settings.player)
        if not player:
            return {'CANCELLED'}
        for axis in ('x', 'z'):
            width = getattr(settings, 'max_' + axis) - getattr(settings, 'min_' + axis)
            minimum = math.floor(getattr(player, axis)) - width // 2
            setattr(settings, 'min_' + axis, minimum)
            setattr(settings, 'max_' + axis, minimum + width)
        if player.dimension:
            settings.dimension = player.dimension
        return {'FINISHED'}


class MCBEDROCK_OT_world_log(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_log'
    bl_label = 'View Log'

    def execute(self, context):
        path = context.scene.mcbedrock_world_exporter.log_path
        if os.path.isfile(path):
            bpy.ops.wm.path_open(filepath=path)
        else:
            self.report({'INFO'}, 'No MiEx log yet')
        return {'FINISHED'}



CLASSES = (MCBEDROCK_PG_world_entry, MCBEDROCK_PG_world_pack, MCBEDROCK_PG_world_exporter,
    MCBEDROCK_UL_worlds, MCBEDROCK_UL_world_packs, MCBEDROCK_OT_world_job,
    MCBEDROCK_OT_world_cancel, MCBEDROCK_OT_world_import, MCBEDROCK_OT_world_water, MCBEDROCK_OT_world_outlines, MCBEDROCK_OT_world_materials, MCBEDROCK_OT_plain_water_color, MCBEDROCK_OT_world_pack,
    MCBEDROCK_OT_world_frame, MCBEDROCK_OT_world_scale_check, MCBEDROCK_OT_world_scale_apply,
    MCBEDROCK_OT_world_center, MCBEDROCK_OT_world_log)


def register():
    from . import world_exporter_workspace, world_exporter_canvas
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mcbedrock_world_exporter = bpy.props.PointerProperty(type=MCBEDROCK_PG_world_exporter)
    bpy.app.handlers.load_pre.append(_before_load)
    if _after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_after_load)
    from ..builders import water_contact
    water_contact.register()
    world_exporter_canvas.register()
    world_exporter_workspace.register()


def unregister():
    from . import world_exporter_workspace
    _before_load(None)
    if bpy.app.timers.is_registered(_poll):
        bpy.app.timers.unregister(_poll)
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)
    if _after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_after_load)
    from ..builders import water_contact
    water_contact.unregister()
    world_exporter_workspace.unregister()
    from . import world_exporter_canvas
    world_exporter_canvas.unregister()
    if hasattr(bpy.types.Scene, 'mcbedrock_world_exporter'):
        del bpy.types.Scene.mcbedrock_world_exporter
    for cls in reversed(CLASSES):
        # PropertyGroup classes need not be exposed as bpy.types attributes.
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
