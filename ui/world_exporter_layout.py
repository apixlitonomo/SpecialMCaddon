"""World Exporter sidebar, built from the Figma concept "MiEx / Area selected".

The map fills the workspace beside one fixed 352 px sidebar on an 8 px grid.
Resource packs are pinned at the top as RP Manager cards with switches. Area,
Look and Save follow as plain disclosures, then the imported-world tools once
an import exists, and More options holds everything rarely changed. Sections
open and close independently, and Import is pinned to the bottom.
"""
import json
import time
from pathlib import Path
import bpy
from .gpu_framework import Rect
from .gpu_framework.theme_adapter import miex_theme

T=miex_theme()
# Sparkle's #282828 hairline separates the sections.
HAIRLINE=T.border
DIVIDER=HAIRLINE
# The map ground sits a step below the #141414 canvas so the two never merge.
MAP_GROUND=(14/255,14/255,14/255,1)
CARD=T.surface
ATTENTION=(245/255,166/255,35/255,1)
DANGER_TEXT=T.danger
SOFT_TEXT=T.text_muted
ICONS=Path(__file__).resolve().parents[1]/'assets'/'ui'/'icons'
# Edition tags from the RP Manager: Java green, Bedrock blue.
JAVA_TAG=(28/255,56/255,15/255,1)
JAVA_TEXT=(109/255,207/255,74/255,1)
BEDROCK_TAG=(28/255,48/255,77/255,1)
BEDROCK_TEXT=(133/255,180/255,1,1)
# Figma grid: 8 px gutters, a 52 px top bar over the map and a 352 px sidebar
# whose last 8 px are its right margin.
GUTTER=8
TOPBAR=52
SIDEBAR=352
SIDE_PAD=8
# The top bar and the sidebar share one panel colour; a light hairline
# subtle grey hairline separates them from the map, as in Figma's own panels.
PANEL=T.surface
EDGE=(48/255,48/255,48/255,1)
# Narrow windows keep this much map; the sidebar gives way, down to SIDE_MIN.
MAP_MIN=240
SIDE_MIN=240
# Floating cards (start, loading) keep their 16 px inset.
PAD=16
HEADER=38
SUBHEADER=32
INDENT=18
PACK_CARD=60
PACK_SEARCH=36
PACK_RELOAD=32   # Reload with new packs, under the search while the stack changed
# Active / Inactive headings over the pack cards, as in the RP Manager.
PACK_GROUP=30
PACKS_VISIBLE=3
EXPORT_BUTTON=44
WATER_SHORT={'ORIGINAL':'Original','DUNGEONS':'Dungeons II','VIBRANT':'Vibrant','PLAIN':'Plain'}
LOOK_TILES={'ORIGINAL':('Original','Keeps the water from your packs'),
            'DUNGEONS':('Dungeons II','Flat deep-blue water with a white shore'),
            'VIBRANT':('Vibrant Visuals','Clear water with pixel ripples'),
            'PLAIN':('Plain','Flat blue with white shores, like key art')}
SECTIONS=(('area','Area'),('look','Look'),('output','Save'))
# Rarely changed settings, nested under More options.
MORE=(('regions','Areas & presets'),('placement','Placement'),('geometry','Geometry'),
      ('advanced','Entities & animation'),('packtools','Pack tools'),('runtime','Runtime'))
DEFAULT_OPEN=('packs','area')
ROW_HEIGHT={'divider':13,'bounds_head':20,'bounds':28,'caption':16,'segmented':30,'option':44,'option_short':32,
            'path':30,'toggle':28,'field':28,'axes':28,'button':30,'pair':30,'preset':32,'link':28}
# Areas this large take a long time and a lot of memory to export.
LARGE_AREA_CHUNKS=4096
# Plain-language versions of the MiEx failures people actually hit.
FRIENDLY_ERRORS=(
    (('locked world','session.lock'),'Minecraft has this world open. Close it in Minecraft, then try again.'),
    (('outofmemory','java heap space','ran out of java memory'),'The export ran out of memory. Raise Memory under More options, Runtime, then try again.'),
    (('choose an existing minecraft world folder',),'That folder is not a Minecraft world. Choose a world from the list.'),
    (('did not produce a complete export',),'The export did not finish. Open the export log for details.'),
)


def geometry(width,height,opened,toolbar=True):
    """Map, sidebar and top bar rects in logical pixels (y up).

    The top bar runs the full width; the map and the sidebar sit below it.
    A 1 px edge separates the top bar from both and the sidebar from the map.
    """
    bar=min(TOPBAR,height) if toolbar else 0
    body=max(1,height-bar-(1 if bar else 0))
    side=max(0,min(SIDEBAR,max(min(SIDE_MIN,width-24-1),width-MAP_MIN-1))) if opened else 0
    column=max(1,width-side-(1 if side else 0))
    return Rect(0,0,column,body),Rect(width-side,0,side,body),Rect(0,height-bar,width,bar)


def ease(state,key,target,duration=.14):
    from .gpu_framework.motion import tween
    return tween(state.motion,'miex.'+key,float(target),duration)


def blend(a,b,t):
    return tuple(x+(y-x)*t for x,y in zip(a,b))


def text_width(r,text,size,weight='Regular'):
    measured=r.text_width(text,size,weight) if hasattr(r,'text_width') else None
    return measured if isinstance(measured,(int,float)) else len(str(text))*size*.56


def fit_text(r,text,width,size,weight='Regular',left=False):
    """Ellipsize to a width; paths keep their tail (left=True)."""
    text=str(text)
    if text_width(r,text,size,weight)<=width:return text
    while text and text_width(r,('…'+text) if left else (text+'…'),size,weight)>width:
        text=text[1:] if left else text[:-1]
    return ('…'+text) if left else (text+'…')


def edition_tag(r,x,y,edition,room=None):
    """RP Manager edition tag (Java / Bedrock) at x, y; returns its width."""
    java=edition in {'Java','Mod'}  # a mod .jar is Java Edition content
    bedrock=edition.startswith('Bedrock')
    fill,ink=(JAVA_TAG,JAVA_TEXT) if java else (BEDROCK_TAG,BEDROCK_TEXT) if bedrock else (T.surface_raised,T.text_muted)
    icon=edition in {'Java','Mod','Bedrock'}
    lead=22 if icon else 7
    width=lead+text_width(r,edition,11,'SemiBold')+7
    if room is not None:width=min(width,room)
    if width<lead+12:return 0
    tag=Rect(x,y,width,19)
    r.rect(tag,fill,radius=3)
    if icon:r.image(Rect(x+6,y+4,11,11),str(ICONS/'java.png'),tint=ink)
    r.text(Rect(x+lead,y,width-lead-4,19),edition,ink,size=11,weight='SemiBold')
    return width


def imported_world(scene):
    """The import the Imported world tools act on, without scanning objects."""
    collections=[c for c in scene.collection.children_recursive if c.get('mcaddon_miex_export')]
    last=scene.get('mcaddon_miex_last_collection','')
    return next((c for c in collections if c.name==last),None) or (collections[-1] if collections else None)


def default_output(settings):
    from . import world_exporter as ui
    return Path(bpy.data.filepath).parent/'MiEx Exports' if bpy.data.filepath else ui.storage()/'exports'


def output_path(settings):
    """The folder the next export is written to, as _snapshot resolves it."""
    return Path(bpy.path.abspath(settings.output)) if settings.output else default_output(settings)


def output_mode(settings,state=None):
    """USER, BLEND or CUSTOM; resolved once per output and .blend path, not every frame."""
    from . import world_exporter as ui
    if not settings.output:return 'BLEND' if bpy.data.filepath else 'USER'
    key=(settings.output,bpy.data.filepath)
    cached=getattr(state,'output_mode_cache',None)
    if cached and cached[0]==key:return cached[1]
    mode='CUSTOM'
    try:
        path=output_path(settings).resolve()
        if path==(ui.storage()/'exports').resolve():mode='USER'
        elif bpy.data.filepath and path==(Path(bpy.data.filepath).parent/'MiEx Exports').resolve():mode='BLEND'
    except OSError:pass
    if state is not None:state.output_mode_cache=(key,mode)
    return mode


def pack_entries(scene,query='',state=None):
    """Active packs first, in priority order, then the rest of the library.

    Entries are (key, active index or -1, discovered index or -1); the key is
    the pack's normalized source path, shared by both lists. Items are looked
    up fresh each frame, so a rescan never leaves a stale reference behind.
    """
    from . import pack_manager as packs
    if scene is None or not hasattr(scene,'mcbedrock_active_packs'):return []
    active=scene.mcbedrock_active_packs
    discovered=scene.mcbedrock_discovered_packs
    query=query.strip()
    signature=(tuple(p.source_path or p.path for p in active),len(discovered),
               scene.get('_mcbedrock_pack_scan_revision',0),query,packs.favorites_revision())
    cached=getattr(state,'pack_entry_cache',None) if state is not None else None
    if cached and cached[0]==signature:return cached[1]
    library={packs._normalized_path(p.path):i for i,p in enumerate(discovered)}
    entries=[];seen=set()
    for i,p in enumerate(active):
        key=packs._normalized_path(p.source_path or p.path)
        seen.add(key)
        if packs._pack_matches_search(p,query):entries.append((key,i,library.get(key,-1)))
    # The stack keeps its priority order; favourites lead the rest of the library.
    for i,p in packs.favorites_first(list(enumerate(discovered))):
        key=packs._normalized_path(p.path)
        if key in seen:continue
        seen.add(key)
        if packs._pack_matches_search(p,query):entries.append((key,-1,i))
    if state is not None:state.pack_entry_cache=(signature,entries)
    return entries


def saved_presets(state):
    """Preset files, newest first; listed at most once a second."""
    now=time.monotonic()
    cached=getattr(state,'preset_cache',None)
    if cached and now-cached[0]<1:return cached[1]
    from . import world_exporter as ui
    items=[]
    try:
        files=sorted((ui.storage()/'presets').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        for path in files[:24]:
            items.append((str(path),path.stem,time.strftime('%d %b %Y',time.localtime(path.stat().st_mtime))))
    except OSError:pass
    state.preset_cache=(now,items)
    return items


def area_rows(settings,state,add,scene):
    if not state.has_selection:
        add(('note','Drag across the map to mark the part of the world you want in Blender.'))
        add(('button','Mark an area at the map center',('reset_selection',)))
        return
    add(('bounds_head',))
    for axis in 'xzy':add(('bounds',axis.upper(),'min_'+axis,'max_'+axis))
    excluding=state.mode=='EXCLUDE'
    add(('pair',('Done excluding' if excluding else 'Exclude chunks',('mode','BOX' if excluding else 'EXCLUDE')),
         ('Clear area',('clear_selection',))))


def look_rows(settings,state,add,scene):
    add(('caption','Render engine'))
    add(('segmented','engine',tuple((label,('engine_set',key),settings.engine==key)
                                    for key,label in (('EEVEE','EEVEE'),('CYCLES','Cycles')))))
    add(('caption','Water'))
    for item in settings.bl_rna.properties['water_preset'].enum_items:
        title,subtitle=LOOK_TILES.get(item.identifier,(item.name,item.description))
        add(('option',title,subtitle,('water_preset',item.identifier),settings.water_preset==item.identifier,True))
    if settings.water_preset=='PLAIN':
        add(('button','Water color…',('water_color',)))
        add(('toggle','Biome colors','plain_water_biome'))
        add(('toggle','Waves','plain_water_waves'))
        add(('note','One flat color, recolored per biome. A white line runs along every shore and around anything you place in the water.'))
    elif settings.water_preset!='ORIGINAL':
        add(('caption','Whitewater'))
        add(('segmented','whitewater_style',tuple((label,('whitewater_set',key),settings.whitewater_style==key)
                                                 for key,label in (('MATCH','Match water'),('SIMPLE','Simple outline')))))
        add(('note','Around everything in the water, including characters and props you add later.'))
    add(('caption','Edges'))
    add(('toggle','Outlines','outlines'))
    add(('note','Soft block edges that catch the light, as in the Minecraft trailers.'))


def output_rows(settings,state,add,scene):
    mode=output_mode(settings,state)
    saved=bool(bpy.data.filepath)
    add(('option','Sparkle folder','Kept in Blender\'s user data',('output_mode','USER'),mode=='USER',True))
    add(('option','Next to the .blend','A MiEx Exports folder' if saved else 'Save the .blend first',
         ('output_mode','BLEND'),mode=='BLEND',saved))
    add(('option','Choose a folder…','',('browse','output'),mode=='CUSTOM',True))
    add(('path',str(output_path(settings)),('browse','output')))
    add(('toggle','Import into Blender when done','auto_import'))


def imported_rows(settings,state,add,scene):
    style=WATER_SHORT.get(settings.water_preset,'')
    add(('button','Restore original water' if settings.water_preset=='ORIGINAL' else 'Apply '+style+' water',('apply_water',)))
    imported=imported_world(scene) if scene is not None and hasattr(scene,'collection') else None
    outlined=bool(imported is not None and imported.get('mc_world_outlines'))
    add(('button','Remove outlines' if outlined else 'Add outlines',('world_outlines',not outlined)))
    if imported is not None and imported.get('mc_world_trailer_materials'):
        # Trailer materials are no longer offered; worlds that have them can drop them.
        add(('button','Restore original materials',('world_materials',False)))
    add(('pair',('Frame world',('frame_world',)),('Check scale',('check_world_scale',))))
    if settings.water_preset=='DUNGEONS':add(('note','Apply water again after moving shoreline objects.'))


def region_rows(settings,state,add,scene):
    add(('field','This area','region_name'))
    if state.has_selection:
        add(('pair',('Add another area',('add_region',)),('Saved areas…',('menu','regions'))))
    add(('field','Preset name','preset_name'))
    add(('pair',('Save preset',('save_preset',)),('Load preset…',('browse','preset'))))
    for path,name,stamp in saved_presets(state)[:6]:add(('preset',name,stamp,'',('load_preset',path)))


def placement_rows(settings,state,add,scene):
    add(('toggle','Center at the origin','center'))
    # Centered exports place X and Z themselves; Y always uses its offset.
    auto=settings.center
    add(('axes',('X','offset_x',auto),('Z','offset_z',auto)))
    add(('axes',('Y','offset_y',False),None))
    add(('field','Chunk size','chunk_size'))


def geometry_rows(settings,state,add,scene):
    for label,prop in [('Optimize faces','run_optimiser'),('Cull underground caves','remove_caves'),('Fill cave openings','fill_caves'),
                       ('Individual blocks','individual'),('Distant level of detail','enable_lod')]:
        add(('toggle',label,prop))
    if settings.enable_lod:
        for label,prop in [('LOD width','lod_width'),('LOD depth','lod_depth'),('Y detail','lod_detail'),('Base level','lod_level')]:
            add(('field',label,prop))
    if state.has_selection:
        editing=state.mode=='FOREGROUND'
        add(('button','Done editing groups' if editing else 'Edit foreground groups',('mode','BOX' if editing else 'FOREGROUND')))


def entity_rows(settings,state,add,scene):
    add(('toggle','Animated blocks','block_animations'))
    add(('toggle','Entities and item frames','entities'))
    if settings.entities:
        for label,prop in [('Entity IDs','entity_types'),('Simulate IDs','entity_simulate'),('Spawn rules','entity_spawn'),
                           ('Start frame','entity_start'),('End frame','entity_end'),('Frames per second','entity_fps'),
                           ('Random seed','entity_seed'),('Spawn density','entity_density'),('Sunlight','entity_light')]:
            add(('field',label,prop))


def packtool_rows(settings,state,add,scene):
    add(('toggle',"Use the world's own packs",'world_packs'))
    add(('field','Generated pack','tool_pack_name'))
    add(('field','Atlas repeats','atlas_repeats'));add(('field','Atlas padding','atlas_padding'))
    add(('pair',('Texture atlas',('job','ATLAS')),('PBR textures',('job','PBR'))))
    add(('button',Path(settings.modpack_path).name if settings.modpack_path else 'Choose modpack folder…',('browse','modpack_path')))
    add(('button','Extract modpack textures',('job','MODPACK')))


def runtime_rows(settings,state,add,scene):
    for label,prop in [('Memory GB','memory'),('Java override','java'),('Exporter override','jar')]:add(('field',label,prop))
    add(('toggle','Reduce interface motion','reduce_motion'))
    add(('pair',('Update base pack',('job','BASE')),('Check runtime',('job','CHECK'))))
    add(('button','Resume map' if getattr(state,'paused',False) else 'Pause map',('pause',)))
    add(('link','Open export log',('log',)))


BUILDERS={'area':area_rows,'look':look_rows,'output':output_rows,'imported':imported_rows,
          'regions':region_rows,'placement':placement_rows,'geometry':geometry_rows,'advanced':entity_rows,
          'packtools':packtool_rows,'runtime':runtime_rows}


def panel_rows(settings,state,scene=None):
    """Area, Look and Save, the imported world when there is one, then More options.

    Headers are ('section', label, key, opened, level). A header owns the rows
    after it until a header of the same or a higher level, or a divider, so More
    options holds its own nested sections. Rows inside a section are 8 px apart.
    """
    scene=scene if scene is not None else getattr(settings,'id_data',None)
    rows=[]

    def section(key,label,level=0):
        opened=key in state.sections
        rows.append(('section',label,key,opened,level))
        # Sample every frame so a section that was open folds from fully open.
        if reveal(state,key,opened)<=.01 and not opened:return False
        if key in BUILDERS:
            content=[]
            BUILDERS[key](settings,state,content.append,scene)
            for row in content:rows.extend((('gap',8),row))
            # Nested sections end on a gap before the next nested header.
            if level and content:rows.append(('gap',8))
        return True

    for index,(key,label) in enumerate(SECTIONS):
        if index:rows.append(('divider',))
        section(key,label)
    if scene is not None and hasattr(scene,'collection') and imported_world(scene) is not None:
        rows.append(('divider',))
        section('imported','Imported world')
    rows.append(('divider',))
    if section('more','More options'):
        for key,label in MORE:section(key,label,1)
    return rows


def reveal(state,key,opened):
    """0..1 open amount of a section; closed sections fold away instead of vanishing."""
    motion=getattr(state,'motion',None)
    if motion is None:return float(opened)
    from .gpu_framework.motion import tween
    return tween(motion,'miex.section.'+key,float(opened),.2)


def inspector_rows(settings,state,scene_name=''):
    """Every row the scrolling sections show for the current state."""
    return panel_rows(settings,state)


def wrap(text,width,size,lines=2):
    """Word-wrap with an Inter width estimate, independent of the GPU renderer."""
    per=max(4,int(width/(size*.55)))
    out=[];current=''
    for word in str(text).split(' '):
        candidate=(current+' '+word).strip()
        if len(word)<=per:
            if len(candidate)<=per or not current:current=candidate
            else:out.append((current,' '));current=word
            continue
        # A path or exception name longer than a line breaks by character, keeping its end.
        if current and per-len(current)-1>=8:
            room=per-len(current)-1
            out.append((current+' '+word[:room],''));word=word[room:]
        elif current:out.append((current,' '))
        while len(word)>per:out.append((word[:per],''));word=word[per:]
        current=word
    out.append((current,''))
    if len(out)>lines:
        # Too long: the last line keeps the end (a file name, "see the log") instead of the middle.
        rest=''.join(line+join for line,join in out[lines-1:]).strip()
        out=out[:lines-1]+[('…'+rest[-(per-1):].lstrip(),'')]
    return [line if len(line)<=per else line[:per-1]+'…' for line,_ in out]


def row_height(row,width):
    kind=row[0]
    if kind=='gap':return row[1]
    if kind=='section':return SUBHEADER if row[4] else HEADER
    if kind=='note':return 16*len(wrap(row[1],width,11.5,lines=4))
    if kind=='option':return ROW_HEIGHT['option'] if row[2] else ROW_HEIGHT['option_short']
    return ROW_HEIGHT.get(kind,30)


def status_tone(settings,busy,reason,warning=False):
    if settings.status.startswith(('Failed','Import failed')):return T.danger
    if busy or settings.status.startswith('Importing'):return T.accent
    if settings.status.startswith(('Imported','Export saved','Export ready','Generated')):return T.success
    return ATTENTION if reason or warning else T.success


def friendly(status):
    """Name the problem and the way out instead of echoing a Java exception."""
    lowered=status.lower()
    for needles,message in FRIENDLY_ERRORS:
        if any(needle in lowered for needle in needles):return message
    return status


def ready_summary(settings,scene):
    """Warn only about size; the sections already show what Export produces."""
    from .world_exporter_canvas import selection_chunks
    chunks=selection_chunks(settings)
    if chunks>LARGE_AREA_CHUNKS:
        return f'Large area: {chunks:,} chunks. Export can take a long time; raise Memory under More options, Runtime if it fails.',True
    return f'Ready to export {chunks:,} chunk'+('' if chunks==1 else 's'),False


def sweep(r,state,track):
    """Indeterminate progress: a short accent segment travelling along a track."""
    if state.motion.get('_motion_disabled'):
        r.rect(track,T.accent,radius=track.height/2)
        return
    span=track.width*.36
    x0=track.x+(track.width+span)*((time.monotonic()%1.4)/1.4)-span
    left=max(track.x,x0);right=min(track.right,x0+span)
    if right>left:r.rect(Rect(left,track.y,right-left,track.height),T.accent,radius=track.height/2)
    # Jobs can run for minutes; the canvas timer moves the sweep at 30 fps, not 60.
    state.motion['_motion_sweep']=True


RESULTS=('Imported','Import failed','Export saved','Export ready','Failed','Cancelled','Generated')


def current_message(settings,state):
    """The last action's message; the next job result replaces it."""
    message=getattr(state,'message','')
    if not message:return ''
    if getattr(state,'message_status',None) is None:state.message_status=settings.status
    if state.message_status!=settings.status and settings.status.startswith(RESULTS):
        state.message='';return ''
    return message


def footer_state(settings,state,width,busy,reason,scene):
    """Status text, tone and height. Idle shows Import alone; status
    lines appear above it only when there is something to say."""
    result=settings.status.startswith(RESULTS)
    warning=failed=False
    message=current_message(settings,state)
    if busy or result and not message:
        status=settings.status
        failed=settings.status.startswith(('Failed','Import failed'))
    # The last action's confirmation, or why it did nothing.
    elif message:status,warning=message,state.message_warning
    elif reason:status=reason
    elif state.notice:status=state.notice
    elif state.has_selection and scene is not None:
        status,warning=ready_summary(settings,scene)
        if not warning:status=''
    else:status=''
    if failed:status=friendly(status.removeprefix('Failed: '))
    if busy:return status,[status],failed,warning,EXPORT_BUTTON+GUTTER+46
    lines=wrap(status,width-16,11.5,lines=3) if status else []
    return status,lines,failed,warning,EXPORT_BUTTON+(GUTTER+4+16*len(lines) if lines else 0)


def busy_detail(settings,scene):
    """The busy footer's second line: world and top-priority pack."""
    packs=getattr(scene,'mcbedrock_active_packs',()) if scene is not None else ()
    top=(packs[0].name+' on top') if len(packs) else 'Vanilla textures'
    return ' · '.join(part for part in (settings.world_name,top) if part)


def draw_footer(paint,settings,state,column,busy,reason,scene=None):
    """Import pinned to the bottom, with any status above it."""
    r=paint.r
    status,lines,failed,warning,height=footer_state(settings,state,column.width,busy,reason,scene)
    button=Rect(column.x,column.y,column.width,EXPORT_BUTTON)
    x=column.x;width=column.width
    from . import world_exporter as ui
    if busy:
        top=button.top+GUTTER+46
        r.text(Rect(x,top-18,width,18),fit_text(r,status,width,12.5,'SemiBold'),T.text,size=12.5,weight='SemiBold')
        r.text(Rect(x,top-36,width,16),fit_text(r,busy_detail(settings,scene),width,11),T.text_muted,size=11)
        # A 2 px blue segment sweeps along the track while a job runs.
        track=Rect(x,button.top+GUTTER+2,width,2)
        r.rect(track,T.surface_raised,radius=1)
        sweep(r,state,track)
        importing=settings.status.startswith('Importing') or getattr(state,'importing',False)
        exporting=ui.busy() and ui._job.get('action')=='EXPORT'
        # Final Blender work cannot be cancelled; say so instead of offering Cancel.
        label='Finishing import…' if importing else 'Cancel export' if exporting else 'Cancel'
        paint.button(button,label,('cancel',),False,not importing,size=13,weight='SemiBold')
        return height
    if lines:
        tone=status_tone(settings,busy,reason,warning)
        if status and status==getattr(state,'message',''):tone=ATTENTION if warning else T.success
        success=settings.status.startswith(('Imported','Export saved','Generated'))
        if settings.status!=getattr(state,'last_status',settings.status) and success:
            state.success_at=time.monotonic()
        top=button.top+GUTTER+4+16*len(lines)
        dot=(x+4,top-8)
        started=getattr(state,'success_at',None)
        if started is not None and not state.motion.get('_motion_disabled'):
            progress=(time.monotonic()-started)/.7
            if 0<=progress<1:
                size=8+16*(1-(1-progress)**3)
                r.rect(Rect(dot[0]-size/2,dot[1]-size/2,size,size),(0,0,0,0),radius=size/2,
                       border=T.success[:3]+(.7*(1-progress),),border_width=1.5)
                state.motion['_motion_active']=True
        r.rect(Rect(dot[0]-4,dot[1]-4,8,8),tone,radius=4)
        for i,line in enumerate(lines):
            r.text(Rect(x+16,top-16-16*i,width-16,16),line,DANGER_TEXT if failed else SOFT_TEXT,size=11.5)
    state.last_status=settings.status
    label='Import' if settings.auto_import else 'Export USD';cmd=('job','EXPORT');enabled=not reason
    follow_up=None
    if settings.status.startswith('Importing'):label='Importing…';enabled=False
    elif getattr(state,'failed',False) and state.loaded:follow_up=('Reload map',('reload',))
    elif settings.status.startswith('Imported'):follow_up=('View in Layout',('layout',))
    elif settings.last_export and settings.status.startswith('Import failed'):follow_up=('Retry import',('import',))
    elif failed:follow_up=('View log',('log',))
    elif settings.last_export:follow_up=('Import saved',('import',))
    if not follow_up:
        paint.button(button,label,cmd,True,enabled,size=13,weight='SemiBold')
        return height
    # The follow-up sits beside the primary in raised grey; each gets the width its label needs.
    primary=min(width-GUTTER-72,max((width-GUTTER)/2,text_width(r,label,13,'SemiBold')+28))
    paint.button(Rect(x,button.y,primary,EXPORT_BUTTON),label,cmd,True,enabled,size=13,weight='SemiBold',pad=8)
    paint.button(Rect(x+primary+GUTTER,button.y,width-GUTTER-primary,EXPORT_BUTTON),*follow_up,size=12,pad=8)
    return height


def draw_header(paint,state,rect,row,indent=0):
    """Disclosure: a 10 px chevron and the section name; nested ones step in 18 px."""
    r=paint.r
    _,label,key,opened,level=row
    action=('section',key)
    x=rect.x+indent
    paint.ring(Rect(x-4,rect.y+3,rect.right-x+4,rect.height-6),action)
    paint.icon(Rect(x,rect.y,10,rect.height),'chevron',T.text,10,rotation=90*(1-reveal(state,key,opened)))
    size,weight=(12,'Medium') if level else (12.5,'SemiBold')
    r.text(Rect(x+18,rect.y,max(0,rect.right-x-18),rect.height),label,T.text,size=size,weight=weight)
    paint.target(rect,action)


def pack_block(state,scene,limit):
    """Natural height of the pinned Resource packs block and of its card list."""
    entries=pack_entries(scene,getattr(state,'pack_search',''),state)
    rows=pack_rows(state,entries)
    natural=sum(PACK_GROUP if row[0]=='group' else PACK_CARD for row in rows)+GUTTER*max(0,len(rows)-1) if rows else 20
    # The headings and three cards by default; dragging the line below the list sets its own height.
    wanted=getattr(state,'pack_list_height',None)
    smallest=PACK_GROUP+GUTTER+PACK_CARD
    groups=max(1,sum(row[0]=='group' for row in rows))
    visible=groups*(PACK_GROUP+GUTTER)+PACKS_VISIBLE*(PACK_CARD+GUTTER)-GUTTER if wanted is None else wanted
    listed=max(min(natural,smallest),min(natural,visible,limit))
    reload=PACK_RELOAD+GUTTER if getattr(state,'packs_changed',False) else 0
    return HEADER+GUTTER+PACK_SEARCH+GUTTER+reload+listed,listed,natural,entries


def pack_caret_visible(state,now=None):
    """The pack search caret's blink phase, twice a second."""
    if not getattr(state,'pack_search_focused',False):return False
    now=time.monotonic() if now is None else now
    return int((now-getattr(state,'pack_search_blink',now))*2)%2==0


def draw_pack_search(paint,state,rect):
    """#1C1C1C search bar; the folder adds a pack folder and the arrows rescan."""
    r=paint.r
    focused=getattr(state,'pack_search_focused',False)
    r.rect(rect,T.surface_raised,radius=4,border=T.accent if focused else None)
    paint.icon(Rect(rect.x+12,rect.y,16,rect.height),'search',T.text_muted,16)
    field=Rect(rect.x,rect.y,rect.width-60,rect.height)
    text_box=Rect(rect.x+38,rect.y,rect.width-38-60,rect.height)
    query=getattr(state,'pack_search','')
    shown=query
    while shown and text_width(r,shown,12)>text_box.width-4:shown=shown[1:]
    if focused and getattr(state,'pack_search_select_all',False) and shown:
        r.rect(Rect(text_box.x,rect.y+8,min(text_box.width,text_width(r,shown,12)),rect.height-16),T.accent[:3]+(.25,))
    r.text(text_box,shown or ('' if focused else 'Search resource packs...'),T.text if query else T.text_muted,size=12)
    now=state.motion.get('_motion_time',time.monotonic())
    # The canvas timer redraws when the caret blinks (pack_caret_visible), not every frame.
    if focused and pack_caret_visible(state,now):
        caret=text_box.x+(text_width(r,shown,12) if shown else 0)
        r.rect(Rect(caret+1,rect.y+10,1,rect.height-20),T.text)
    paint.target(field,('pack_search',))
    for offset,icon,cmd,tip in ((-42,'folder',('pack_folder',),'Add resource pack folder'),
                                (-16,'refresh',('scan',),'Scan for resource packs')):
        cell=Rect(rect.right+offset-6,rect.y+4,28,rect.height-8)
        paint.icon(cell,icon,T.text_muted,16)
        paint.ring(cell,cmd);paint.tip(cell,tip);paint.target(cell,cmd)


def draw_pack_card(paint,state,rect,key,item,active):
    """RP Manager pack card: art, edition tag over the name, and the on/off switch."""
    from .pack_manager import _edition_label
    r=paint.r
    on=active>=0
    r.rect(rect,T.selected if on else T.surface_raised,radius=4)
    thumb=Rect(rect.x+12,rect.y+(rect.height-32)/2,32,32)
    from .pack_manager import is_generated_icon,is_mod_kind,mod_pack_icon
    icon,mod=item.icon_path,is_mod_kind(item.kind)
    if mod and (not icon or is_generated_icon(icon)):
        icon=mod_pack_icon(item)  # as on the Packs page: one of the mod's own models
    rendered=Path(icon or '').name.startswith('modrender_')
    if not icon or not r.image(thumb,icon,radius=3,fit=mod,nearest=not rendered):
        r.rect(thumb,T.surface_raised,radius=3)
        r.text(thumb,'Mod' if mod else 'RP',T.text_muted,size=11,align='CENTER')
    switch=Rect(rect.right-12-36,rect.y+(rect.height-20)/2,36,20)
    x=thumb.right+12
    room=max(0,switch.x-12-x)
    from .pack_manager import is_favorite
    if is_favorite(item.path) and room>40:
        paint.icon(Rect(switch.x-24,rect.y,12,rect.height),'star-fill',T.accent_hover,12)
        room-=18
    edition_tag(r,x,rect.top-29,_edition_label(item.kind),room)
    name=item.name or Path(item.path).name
    r.text(Rect(x,rect.top-50,room,16),fit_text(r,name,room,13,'SemiBold'),T.text,size=13,weight='SemiBold')
    action=('pack_card',key)
    paint.ring(rect,action)
    paint.tip(rect,(name+' · top priority' if active==0 else name)+('. Drag to reorder, right-click for more' if on else ''))
    paint.target(rect,action)
    paint.switch(switch,('pack_toggle',key),on,'pack.'+key)


def draw_packs(paint,settings,state,box,scene,listed,natural,entries):
    """Pinned Resource packs: disclosure, search, then the stack and library as cards."""
    r=paint.r
    opened='packs' in state.sections
    header=Rect(box.x,box.top-HEADER,box.width,HEADER)
    draw_header(paint,state,header,('section','Resource packs','packs',opened,0))
    state.pack_cards=[];state.pack_list_rect=Rect(0,0,0,0)
    shown=reveal(state,'packs',opened)
    body=Rect(box.x,box.y,box.width,max(0,header.y-box.y))
    if shown<=.01 or body.height<=0:return
    opacity=r.opacity
    r.push_clip(body);paint.clip=body;r.opacity=opacity*shown
    try:
        search=Rect(box.x,header.y-GUTTER-PACK_SEARCH,box.width,PACK_SEARCH)
        draw_pack_search(paint,state,search)
        if getattr(state,'packs_changed',False):
            # The map still shows the packs it was loaded with: one reload takes every change.
            reload=Rect(box.x,search.y-GUTTER-PACK_RELOAD,box.width,PACK_RELOAD)
            paint.button(reload,'Reload with new packs',('reload',),icon='refresh')
            paint.tip(reload,'The map and 3D view still show the packs they were loaded with')
            search=reload
        area=Rect(box.x,search.y-GUTTER-listed,box.width,listed)
        visible=area.intersect(body)
        if visible.height<=0:return
        if not entries:
            message='No matching packs' if getattr(state,'pack_search','') else 'No resource packs found. Scan or add a folder.'
            r.text(Rect(area.x,area.top-20,area.width,20),message,T.text_muted,size=12)
            return
        state.scroll_max['packs']=max(0,natural-listed)
        state.scroll['packs']=max(0,min(state.scroll.get('packs',0),state.scroll_max['packs']))
        state.pack_list_rect=visible
        r.push_clip(visible);paint.clip=visible
        try:draw_pack_list(paint,state,area,scene,entries)
        finally:r.pop_clip();paint.clip=body
        if state.scroll_max['packs']:
            if state.scroll['packs']>1:paint.fade(Rect(area.x,area.top-14,area.width,14),PANEL,True)
            if state.scroll['packs']<state.scroll_max['packs']-1:paint.fade(Rect(area.x,area.y,area.width,14),PANEL,False)
    finally:
        r.opacity=opacity;r.pop_clip();paint.clip=None


def pack_order(state,entries):
    """Card order, with a dragged active pack moved to the slot under the pointer."""
    order=list(range(len(entries)))
    drag=state.drag if state.drag and state.drag[0]=='pack' else None
    if drag is None or not drag[3].get('moved'):return order,None
    source=next((i for i,entry in enumerate(entries) if entry[0]==drag[2]),None)
    actives=sum(1 for entry in entries if entry[1]>=0)
    if source is None or entries[source][1]<0:return order,None
    target=max(0,min(actives-1,drag[3].get('target',source)))
    order.remove(source);order.insert(target,source)
    return order,source


def pack_rows(state,entries,order=None):
    """('group', key, label, count, opened) headings and ('card', entry index) rows.

    Enabled packs sit under Active in priority order, the rest under Inactive.
    A search keeps both groups open so matches never hide.
    """
    order=list(range(len(entries))) if order is None else order
    searching=bool(getattr(state,'pack_search','').strip())
    closed=getattr(state,'pack_groups_closed',set())
    rows=[]
    for group,label,enabled in (('active','Active',True),('inactive','Inactive',False)):
        members=[i for i in order if (entries[i][1]>=0)==enabled]
        if not members:continue
        opened=searching or group not in closed
        rows.append(('group',group,label,len(members),opened))
        if opened:rows.extend(('card',i) for i in members)
    return rows


def draw_pack_group(paint,state,rect,row):
    """RP Manager group heading: chevron, label and the pack count."""
    _,group,label,count,opened=row
    action=('pack_group',group)
    paint.ring(rect,action)
    paint.icon(Rect(rect.x+2,rect.y,10,rect.height),'chevron',T.text,10,
               rotation=90*(1-ease(state,'pack.group.'+group,opened,.2)))
    paint.r.text(Rect(rect.x+20,rect.y,rect.width-60,rect.height),label,T.text,size=12,weight='SemiBold')
    paint.r.text(Rect(rect.x,rect.y,rect.width-4,rect.height),str(count),T.text_muted,size=11,align='RIGHT')
    paint.target(rect,action)


def draw_pack_list(paint,state,area,scene,entries):
    top=area.top+state.scroll.get('packs',0)
    # Where the first Active card starts; a drag measures its target slot from here.
    state.pack_list_top=top-PACK_GROUP-GUTTER
    order,dragged=pack_order(state,entries)
    lifted=None
    y=top
    for row in pack_rows(state,entries,order):
        if row[0]=='group':
            rect=Rect(area.x,y-PACK_GROUP,area.width,PACK_GROUP)
            y-=PACK_GROUP+GUTTER
            if rect.y<=area.top and rect.top>=area.y:draw_pack_group(paint,state,rect,row)
            continue
        index=row[1]
        key,active,library=entries[index]
        rect=Rect(area.x,y-PACK_CARD,area.width,PACK_CARD)
        y-=PACK_CARD+GUTTER
        if index==dragged:
            # The dragged card follows the pointer above the others.
            drag=state.drag[3]
            lifted=(Rect(area.x,drag['card_top']-(drag['start']-state.hover[1])-PACK_CARD,area.width,PACK_CARD),key,active)
            continue
        if rect.y>area.top or rect.top<area.y:continue
        item=scene.mcbedrock_active_packs[active] if active>=0 else scene.mcbedrock_discovered_packs[library]
        draw_pack_card(paint,state,rect,key,item,active)
        state.pack_cards.append((rect.intersect(area),key,item.path if active>=0 else None,rect.top))
    if lifted:
        rect,key,active=lifted
        item=scene.mcbedrock_active_packs[active]
        opacity=paint.r.opacity;paint.r.opacity=opacity*.92
        try:draw_pack_card(paint,state,rect,key,item,active)
        finally:paint.r.opacity=opacity


def draw_row(paint,settings,state,box,row,indent=0,context=None):
    """One section row at its natural height; indent steps nested rows in."""
    r=paint.r
    kind=row[0]
    x=box.x+indent;width=box.width-indent
    if kind=='section':draw_header(paint,state,box,row,indent)
    elif kind=='divider':r.rect(Rect(box.x,box.y+6,box.width,1),DIVIDER)
    elif kind=='bounds_head':
        column=(width-GUTTER)/2
        for i,label in enumerate(('Min','Max')):
            r.text(Rect(x+i*(column+GUTTER),box.y,column,box.height),label,T.text_muted,size=10.5,weight='Medium')
    elif kind=='bounds':
        _,letter,low,high=row
        column=(width-GUTTER)/2
        for i,prop in enumerate((low,high)):
            paint.axis(Rect(x+i*(column+GUTTER),box.y,column,box.height),letter,settings,prop)
    elif kind=='axes':
        column=(width-GUTTER)/2
        for i,cell in enumerate(row[1:]):
            if cell is None:continue
            letter,prop,auto=cell
            paint.axis(Rect(x+i*(column+GUTTER),box.y,column,box.height),letter,settings,prop,auto)
    elif kind=='caption':r.text(Rect(x,box.y,width,box.height),row[1],T.text_muted,size=10.5,weight='Medium')
    elif kind=='segmented':paint.segmented(Rect(x,box.y,width,box.height),row[1],row[2])
    elif kind=='option':paint.option(Rect(x,box.y,width,box.height),*row[1:])
    elif kind=='path':
        field=Rect(x,box.y,width,box.height)
        r.rect(field,T.surface_raised,radius=4)
        browse=text_width(r,'Change',12)
        r.text(Rect(field.x+10,field.y,field.width-browse-30,field.height),
               fit_text(r,row[1],field.width-browse-30,11.5,left=True),T.text_muted,size=11.5)
        r.text(Rect(field.x,field.y,field.width-10,field.height),'Change',T.text,size=12,align='RIGHT')
        paint.ring(field,row[2])
        paint.target(field,row[2]);paint.tip(field,row[1])
    elif kind=='toggle':paint.toggle(Rect(x,box.y,width,box.height),row[1],settings,row[2])
    elif kind=='field':paint.field(Rect(x,box.y,width,box.height),row[1],settings,row[2])
    elif kind=='button':
        paint.button(Rect(x,box.y,width,box.height),row[1],row[2],size=12)
        if row[2][0]=='menu':state.anchors[row[2][1]]=(x,box.y-4)
    elif kind=='pair':
        half=(width-GUTTER)/2
        for i,(label,cmd) in enumerate(row[1:]):
            paint.button(Rect(x+i*(half+GUTTER),box.y,half,box.height),label,cmd,enabled=cmd!=('noop',),size=12,pad=6)
            if cmd[0]=='menu':state.anchors[cmd[1]]=(x,box.y-4)
    elif kind=='note':
        for i,line in enumerate(wrap(row[1],width,11.5,lines=4)):
            r.text(Rect(x,box.top-16*(i+1),width,16),line,T.text_muted,size=11.5)
    elif kind=='preset':paint.card(Rect(x,box.y,width,box.height),*row[1:],kind='preset')
    elif kind=='link':
        paint.ring(Rect(x-4,box.y,width+4,box.height),row[2])
        paint.icon(Rect(x,box.y,14,box.height),'file-text',T.text_muted,14)
        r.text(Rect(x+22,box.y,width-22,box.height),row[1],T.text,size=12)
        paint.target(Rect(x,box.y,width,box.height),row[2])


def section_tree(rows):
    """Nest rows under their headers: a header owns the rows after it until a
    header of the same or a higher level, or a divider."""
    root=[];stack=[(-1,root)]
    for row in rows:
        if row[0]=='section':
            while stack[-1][0]>=row[4]:stack.pop()
            node=(row,[])
            stack[-1][1].append(node);stack.append((row[4],node[1]))
        else:
            if row[0]=='divider':del stack[1:]
            stack[-1][1].append((row,None))
    return root


def panel_frames(state,rows,width):
    """(row, top offset, height, fold window, opacity, indent) with sections folding open and shut.

    A section's rows keep their natural spacing; the window below its header
    grows to the revealed share of their height and clips them, so the headers
    beneath slide instead of jumping. Nested windows clip within their parent's.
    """
    frames=[]

    def indent_of(row,indent):
        return INDENT*row[4] if row[0]=='section' else indent

    def measure(nodes,indent):
        total=0.
        for row,children in nodes:
            total+=row_height(row,width-indent_of(row,indent))
            if children is not None:total+=measure(children,INDENT*row[4])*reveal(state,row[2],row[3])
        return total

    def place(nodes,offset,window,alpha,indent):
        for row,children in nodes:
            own=indent_of(row,indent)
            height=row_height(row,width-own)
            frames.append((row,offset,height,window,alpha,own))
            offset+=height
            if children is None:continue
            shown=reveal(state,row[2],row[3])
            natural=measure(children,INDENT*row[4])
            inner=None
            if shown<.999 or window is not None:
                inner=(offset,offset+natural*shown)
                if window is not None:inner=(max(inner[0],window[0]),min(inner[1],window[1]))
            place(children,offset,inner,alpha*shown,INDENT*row[4])
            offset+=natural*shown
        return offset

    total=place(section_tree(rows),0.,None,1.,0)
    return frames,total


def draw_panel(paint,settings,state,box,rows,side,context=None):
    """Scrolling column of sections, clipped to its box."""
    r=paint.r
    frames,total=panel_frames(state,rows,box.width)
    state.scroll_max[side]=max(0,total-box.height)
    state.scroll[side]=max(0,min(state.scroll[side],state.scroll_max[side]))
    top=box.top+state.scroll[side]
    opacity=r.opacity
    r.push_clip(box);paint.clip=box
    try:
        for row,offset,height,window,alpha,indent in frames:
            if alpha<=.01 or row[0]=='gap':continue
            rect=Rect(box.x,top-offset-height,box.width,height)
            if rect.y>box.top or rect.top<box.y:continue
            clip=None
            if window is not None:
                clip=Rect(box.x,top-window[1],box.width,window[1]-window[0]).intersect(box)
                if clip.height<=0:continue
                r.push_clip(clip);paint.clip=clip
            r.opacity=opacity*alpha
            try:draw_row(paint,settings,state,rect,row,indent,context)
            finally:
                r.opacity=opacity
                if clip is not None:r.pop_clip();paint.clip=box
    finally:
        r.pop_clip();paint.clip=None
    if state.scroll_max[side]:
        # An eased fade hints at more content above or below, and a 3 px thumb shows the position.
        if state.scroll[side]>1:paint.fade(Rect(box.x,box.top-18,box.width,18),PANEL,True)
        if state.scroll[side]<state.scroll_max[side]-1:paint.fade(Rect(box.x,box.y,box.width,18),PANEL,False)
        thumb=max(28,box.height*box.height/(box.height+state.scroll_max[side]))
        y=box.top-thumb-(box.height-thumb)*state.scroll[side]/state.scroll_max[side]
        r.rect(Rect(box.right+3,y,3,thumb),T.text_muted[:3]+(.35,),radius=1.5)


def draw_inspector(paint,settings,state,box,context):
    """The sidebar: pinned Resource packs, the sections, and Import pinned below."""
    if box.width<=0:return
    r=paint.r
    r.rect(box,PANEL)
    column=Rect(box.x+SIDE_PAD,box.y+SIDE_PAD,max(0,box.width-2*SIDE_PAD),max(0,box.height-SIDE_PAD))
    from . import world_exporter as ui
    from .world_exporter_canvas import export_reason
    scene=getattr(context,'scene',None)
    reason=export_reason(settings,state)
    busy=ui.foreground_busy() or bool(state.pending)
    footer=footer_state(settings,state,column.width,busy,reason,scene)[4]
    # The pack list keeps room for a few section headers below it.
    room=column.height-footer-GUTTER-17-HEADER-GUTTER-PACK_SEARCH-GUTTER-4*HEADER
    if getattr(state,'packs_changed',False):room-=PACK_RELOAD+GUTTER
    total,listed,natural,entries=pack_block(state,scene,room)
    shown=reveal(state,'packs','packs' in state.sections)
    pinned_height=HEADER+(total-HEADER)*shown
    pinned=Rect(column.x,column.top-pinned_height,column.width,pinned_height)
    draw_packs(paint,settings,state,pinned,scene,listed,natural,entries)
    divider=pinned.y-GUTTER-1
    # The line under the pack list is a handle: drag it to show more or fewer packs.
    resizing=bool(state.drag and state.drag[0]=='pack_resize')
    grip=ease(state,'pack.resize',resizing,.12)
    r.rect(Rect(column.x,divider,column.width,1),blend(DIVIDER,T.accent,grip))
    if 'packs' in state.sections and entries:
        handle=Rect(column.x,divider-5,column.width,11)
        state.pack_resize_rect=handle
        state.pack_list_listed=listed
        paint.tip(handle,'Drag to resize the pack list')
        paint.target(handle,('pack_resize',))
    else:state.pack_resize_rect=Rect(0,0,0,0)
    bottom=column.y+footer+GUTTER
    content=Rect(column.x,bottom,column.width,max(0,divider-GUTTER-bottom))
    draw_panel(paint,settings,state,content,panel_rows(settings,state,scene),'right',context)
    # Drawn last so Tab moves through the sidebar before reaching the export action.
    draw_footer(paint,settings,state,column,busy,reason,scene)
