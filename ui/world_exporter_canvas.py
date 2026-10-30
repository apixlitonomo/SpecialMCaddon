"""GPU/BLF world browser, selection and export controls in a full workspace.

No scene geometry or image datablocks are created for the map. MiEx supplies
real chunk images and heightmaps; its worker owns all Java communication.
"""
from collections import OrderedDict
import json
import math
from pathlib import Path
import time
import bpy
from .gpu_framework import Rect
from .gpu_framework.renderer import BlenderRenderer
from .gpu_framework.theme_adapter import miex_theme
from ..exporters.miex.map_model import MapView, drag_bounds, decode_chunk
from ..exporters.miex.map_atlas import affected_pages, PAGE_CHUNKS
from ..exporters.miex.worlds import dimension_key
from . import world_map_renderer
from .world_exporter_preview import Preview

KEY = 'mcaddon_world_exporter_workspace'
running = set()
_states = {}
# area pointer -> (window pointer, timer) of each live canvas session.
_timers = {}
_handle = None
_enabled = False
# A map hidden this long gives back its GPU pages and the tiles away from its last view.
RELEASE_AFTER = 30.
# Blender's own shortcuts stay live over the canvas (see native_shortcut).
F_KEYS = {'F%d'%i for i in range(1,25)}
# Sparkle's sidebar palette, shared by every exporter surface.
T = miex_theme()
CONTROL_RADIUS = T.radius
PRESET_FIELDS = ('world_path','dimension','min_x','min_y','min_z','max_x','max_y','max_z',
    'center','offset_x','offset_y','offset_z','remove_caves','fill_caves','run_optimiser','individual','regions','region_name','extra_exclusions',
    'chunk_size','enable_lod','lod_width','lod_depth','lod_detail','lod_level','excluded','foreground',
    'block_animations','entities','entity_types','entity_simulate','entity_spawn','entity_start','entity_end',
    'entity_fps','entity_seed','entity_density','entity_light','world_packs','engine','output','auto_import','water_preset',
    'outlines','whitewater_style','plain_water_color','plain_water_biome','plain_water_waves')


class State:
    def __init__(self):
        self.view = MapView()
        self.tiles = OrderedDict()
        self.textures = OrderedDict()
        self.prepared_pages = OrderedDict()
        self.dirty_pages = set()
        self.map_pages = set()
        self.worlds_scanned = False
        self.world_bounds = None
        self.epoch = 0
        self.loaded = False
        self.ui_ready = False
        self.paused = False
        self.failed = False
        self.pending = None
        self.signature = None
        # The pack stack the map was loaded with; another one waits for Reload.
        self.loaded_stack = None
        self.packs_changed = False
        self.map_rect = Rect(250,46,800,700)
        self.targets = []
        self.hover = (0,0)
        self.drag = None
        self.mode = 'BOX'
        self.scroll = {'right':0,'packs':0}
        self.scroll_max = {'right':0,'packs':0}
        self.edit = None
        self.menu = None
        self.menu_scroll = 0
        self.search = ''
        self.search_focused = False
        self.search_blink_started = 0.0
        self.search_select_all = False
        self.worlds_refresh_started = None
        self.worlds_keyboard_focus = False
        self.advanced = False
        self.tools = False
        self.last_request = 0
        self.size = (0,0)
        self.scale = 1
        self.renderer = None
        self.notice = ''
        self.visible_key = None
        self.visible_chunks = []
        self.draw_stamp = None
        self.inspector_open = False
        self.reveal_started = None
        self.overlay_rects = []
        self.inspector_rect = Rect(0,0,0,0)
        # Open sections; Resource packs and Area start open, as in the design.
        self.sections = {'packs','area'}
        self.snap = False
        self.history = []
        self.future = []
        self.tips = []
        self.json_cache = {}
        self.drag_snapshot = None
        self.native_edit = False
        self.toolbar_rect = Rect(0,0,0,0)
        # Where toolbar and panel menus drop from, keyed by menu name.
        self.anchors = {}
        self.has_selection = False
        self.preset_selection = False
        self.motion = {}
        self.island_rect = Rect(0,0,0,0)
        self.menu_rect = Rect(0,0,0,0)
        self.menu_anchor = (0,0)
        self.menu_index = 0
        self.right_dragged = False
        self.drag_button = None
        self.focus = None
        self.importing = False
        # The pinned Resource packs list: its search text, the cards drawn
        # last frame and the visible list rect for wheel scrolling.
        self.pack_search = ''
        self.pack_search_focused = False
        self.pack_search_select_all = False
        self.pack_search_blink = 0.0
        self.pack_cards = []
        self.pack_list_rect = Rect(0,0,0,0)
        self.pack_list_top = 0.0
        self.pack_entry_cache = None
        # Height the user dragged the pack list to; None keeps three cards.
        self.pack_list_height = None
        # Folded Active / Inactive headings in the pack list.
        self.pack_groups_closed = set()
        self.pack_list_listed = 0.0
        self.pack_resize_rect = Rect(0,0,0,0)
        self.resize_cursor = False
        self.flash = {}
        self.page_switched = None
        self.focus_visible = False
        self.y_hint_until = 0.0
        self.last_status = ''
        self.success_at = None
        self.tile_revision = 0
        self.world_picker_visible = 0.0
        # The 3D preview of the selected area and its Flat | 3D switch at the map's top-left.
        self.preview = Preview()
        self.preview_button = Rect(0,0,0,0)
        # The last action's confirmation, or why it did nothing, shown above Import.
        self.message = ''
        self.message_warning = False
        self.message_status = None
        # The tooltip under the pointer, since when, and whether it is drawn.
        self.tip_text = None
        self.tip_since = 0.0
        self.tip_shown = False
        # When the wheel last scrolled the sidebar: a scroll in progress never edits a field.
        self.wheel_scrolled = -1.0
        self.swept = 0.0
        # Each canvas area showing this scene keeps its own layout (use_area).
        self.area_key = None
        self.area_layouts = {}
        # Since when no window shows this map, and whether its memory went back.
        self.hidden_since = None
        self.released = False

    def visible(self):
        rect=tuple_rect(self.map_rect)
        a=self.view.world(rect[0],rect[1]+rect[3],rect)
        b=self.view.world(rect[0]+rect[2],rect[1],rect)
        key = tuple(math.floor(v/16) for v in (*a,*b,self.view.x,self.view.z))
        if key != self.visible_key:
            self.visible_key = key
            self.visible_chunks = self.view.visible(rect)
        return self.visible_chunks


def state(scene):
    # Blender can replace a datablock address during undo. Its session UID
    # remains stable without sharing one world's state with another scene.
    key = scene.session_uid
    if key not in _states:
        _states[key] = State()
    return _states[key]


def reset():
    _release_timers()
    _states.clear()
    running.clear()


def _release_timers():
    """Blender frees canvas sessions without cancel() on file load and add-on unregister;
    remove their timers first so none keeps firing into the window."""
    manager=getattr(bpy.context,'window_manager',None)
    alive={w.as_pointer() for w in manager.windows} if manager else set()
    for window,timer in _timers.values():
        if window in alive:
            try:manager.event_timer_remove(timer)
            except (ReferenceError,RuntimeError,ValueError):pass
    _timers.clear()


# What each canvas area keeps for itself when two show one scene (a second
# window on this tab): layout, hit targets, renderer caches and the pointer.
AREA_FIELDS={'size':lambda:(0,0),'renderer':lambda:None,'map_rect':lambda:Rect(250,46,800,700),'targets':list,'tips':list,
    'overlay_rects':list,'inspector_rect':lambda:Rect(0,0,0,0),'toolbar_rect':lambda:Rect(0,0,0,0),
    'island_rect':lambda:Rect(0,0,0,0),'menu_rect':lambda:Rect(0,0,0,0),'preview_button':lambda:Rect(0,0,0,0),
    'anchors':dict,'pack_cards':list,'pack_list_rect':lambda:Rect(0,0,0,0),'pack_list_top':float,
    'pack_list_listed':float,'pack_resize_rect':lambda:Rect(0,0,0,0),'scroll_max':lambda:{'right':0,'packs':0},
    'world_picker_box':lambda:None,'menu_visible_count':lambda:4,'hover':lambda:(0,0),'tip_text':lambda:None,
    'tip_since':float,'tip_shown':bool,'visible_key':lambda:None,'visible_chunks':list}


def use_area(s,key):
    """Make `key`'s layout current; the first area simply claims the state's."""
    if s.area_key==key:return
    if s.area_key is not None:
        s.area_layouts[s.area_key]={name:getattr(s,name,None) for name in AREA_FIELDS}
        saved=s.area_layouts.get(key) or {name:make() for name,make in AREA_FIELDS.items()}
        for name,value in saved.items():setattr(s,name,value)
    s.area_key=key


def tell(s,text,warning=False):
    """Say what the last action did (or why it did nothing) above Import."""
    s.message=text;s.message_warning=warning;s.message_status=None


def suspend(s):
    """Leaving the workspace drops gestures, popups and hover; the map stays loaded."""
    s.drag=None;s.drag_snapshot=None;s.drag_button=None;s.edit=None;s.menu=None
    s.world_picker_visible=0
    s.motion.get('_motion',{}).pop('worlds.reveal',None)
    s.draw_stamp=None
    s.pack_search_focused=False
    s.pack_entry_cache=None
    s.hover=(-1e9,-1e9);s.tip_text=None;s.tip_shown=False


def release(s):
    """Give back the GPU pages, the 3D preview's GPU copy and the tiles away from the
    last view; what was on screen comes straight back from the kept tiles."""
    s.textures.clear();s.prepared_pages.clear();s.preview.free_gpu();s.renderer=None
    for layout in s.area_layouts.values():layout['renderer']=None
    rect=tuple_rect(s.map_rect)
    near={(x+dx,z+dz) for x,z in s.view.page_keys(rect) for dx in (-1,0,1) for dz in (-1,0,1)}
    for key in [key for key in s.tiles if (key[0]//PAGE_CHUNKS,key[1]//PAGE_CHUNKS) not in near]:del s.tiles[key]
    s.map_pages&=near;s.dirty_pages&=near
    s.tile_revision+=1
    s.released=True


def _release_hidden():
    """Timer: release maps no window has shown for RELEASE_AFTER seconds."""
    now=time.monotonic();again=False
    shown={w.scene.session_uid for w in bpy.context.window_manager.windows
           if w.workspace.get(KEY) and not w.screen.is_temporary}
    for uid,s in list(_states.items()):
        if uid in shown or s.released:continue
        if s.hidden_since is None:s.hidden_since=now
        if now-s.hidden_since>=RELEASE_AFTER:release(s)
        else:again=True
    return RELEASE_AFTER/3 if again else None


def invalidate(scene):
    s = state(scene)
    s.epoch += 1
    s.tiles.clear()
    s.textures.clear()
    s.prepared_pages.clear()
    s.dirty_pages.clear()
    s.map_pages.clear()
    s.failed = False
    s.tile_revision += 1
    s.preview.invalidate()


def signature(settings):
    from .world_exporter_packs import shared_stack
    return (settings.world_path,settings.dimension,settings.world_packs,settings.engine,
            tuple(shared_stack(settings.id_data)))


def world_loaded(scene, info):
    invalidate(scene)
    s = state(scene)
    settings = scene.mcbedrock_world_exporter
    s.signature = signature(settings)
    s.loaded_stack, s.packs_changed = s.signature[4], False
    first_load = not s.loaded
    s.loaded = True
    if first_load:
        s.ui_ready = False
        s.inspector_open = True
        # Every world opens on the flat map.
        s.preview.turn_off()
        s.reveal_started = None
        saved=(settings.min_x,settings.max_x,settings.min_z,settings.max_z)!=(0,15,0,15)
        s.has_selection = s.preset_selection or saved
    s.preset_selection = False
    s.paused = False
    s.history.clear()
    s.drag=None
    bounds=info.get('worldBounds',[])
    s.world_bounds=(bounds[0],bounds[1],bounds[2]+511,bounds[3]+511) if len(bounds)==4 and all(abs(v)<30000000 for v in bounds) else None
    players = [p for p in info.get('players',[]) if dimension_key(p.get('dimension','')) == dimension_key(settings.dimension)]
    if players:
        settings.player = players[0].get('name') or players[0].get('uuid','Player')
    if first_load:
        # Start with a small area at the world origin, independent of player
        # position and the outer region-file bounds. Refreshes keep navigation.
        s.view.x=s.view.z=0
        s.view.scale=min(48,max(4.0,s.map_rect.width/256,s.map_rect.height/256))


def receive_tiles(scene, job, rows):
    s = state(scene)
    if job.get('map_epoch') != s.epoch or signature(scene.mcbedrock_world_exporter) != s.signature:
        return
    received = {}
    for row in rows:
        # The job thread decodes rows ('tile'); raw rows still decode here.
        tile = row['tile'] if 'tile' in row else decode_chunk(row)
        received[row['chunkX'],row['chunkZ']] = tile if any(tile[0][3::4]) else None
    for row in job['chunks']:
        key = row['chunkX'],row['chunkZ']
        s.tiles[key] = received.get(key)
        if s.tiles[key]:
            s.map_pages.add((key[0]//16,key[1]//16))
        for page in affected_pages(key):
            s.dirty_pages.add(page)
            s.prepared_pages.pop(page,None)
    while len(s.tiles)>131072:
        key,_ = s.tiles.popitem(last=False)
        for page in affected_pages(key):
            s.dirty_pages.add(page)
            s.prepared_pages.pop(page,None)
    s.tile_revision+=1
    remaining = sum(key not in s.tiles for key in s.visible())
    settings=scene.mcbedrock_world_exporter
    # Background map arrivals must not erase the result of an explicit operation.
    if not settings.status.startswith(('Imported','Export','Failed','Import failed','Cancelled','Generated')):
        settings.status = (f'Loading map · {remaining} visible chunks remaining' if remaining
            else 'No terrain here · pan or use Go to player' if not any(s.tiles.get(key) for key in s.visible())
            else 'Map ready · drag to select, middle mouse to pan, wheel to zoom')


def receive_preview(scene, job, decoded):
    state(scene).preview.receive(job, decoded)


def job_error(scene, job):
    if job.get('action') == 'MAP':
        state(scene).failed = True
    elif job.get('action') == 'PREVIEW':
        state(scene).preview.fail(job)


def tick(context, s):
    from . import world_exporter as ui
    from .world_exporter_packs import sync_mirror
    sync_mirror(context.scene)
    settings = context.scene.mcbedrock_world_exporter
    current = signature(settings)
    if s.signature != current:
        changed_world = s.signature is None or s.signature[0] != current[0]
        if s.loaded and not changed_world and s.signature[:4] == current[:4] and s.loaded_stack is not None:
            # Packs are often switched several in a row: each switch reloaded the
            # whole world. The map keeps the packs it was loaded with and a Reload
            # button shows; switching back to them hides it again.
            s.packs_changed = current[4] != s.loaded_stack
        else:
            invalidate(context.scene)
            if changed_world:
                s.loaded = False
                s.ui_ready = False
            elif s.loaded and not ui.foreground_busy():
                settings.status='Map paused · Resume map to refresh' if s.paused else 'Refreshing map…'
        s.signature = current
    # Pages are only uploaded by the flat map's draw; the full 3D view never takes them.
    if s.loaded and s.preview.shown<1:world_map_renderer.prepare_pages(s)
    s.preview.sync(s,settings,time.monotonic())
    if ui.busy() or s.importing:
        return
    # The preview first asks for the area's missing map tiles (surface heights), then its rows.
    tiles = s.preview.next_tiles(s) if s.preview.on and s.loaded and not s.pending else []
    rows = s.preview.next_rows() if s.preview.on and s.loaded and not s.pending and not tiles else []
    # Until Reload, the map and the 3D view ask with the packs they were loaded with.
    stack = s.loaded_stack if s.packs_changed else None
    if s.pending:
        action,s.pending = s.pending,None
        ui.start_job(context,action)
    elif tiles:
        ui.start_job(context,'MAP',map_epoch=s.epoch,chunks=[dict(chunkX=x,chunkZ=z) for x,z in tiles],stack=stack)
        s.last_request = time.monotonic()
    elif rows:
        ui.start_job(context,'PREVIEW',preview_epoch=s.preview.epoch,chunks=rows,stack=stack)
    elif s.loaded and not s.preview.on and not s.paused and not s.failed and time.monotonic()-s.last_request>.1:
        rect = s.map_rect
        from itertools import islice
        missing = list(islice((p for p in s.visible() if p not in s.tiles),64))
        if missing:
            ui.start_job(context,'MAP',map_epoch=s.epoch,chunks=[dict(chunkX=x,chunkZ=z) for x,z in missing],stack=stack)
            s.last_request = time.monotonic()


SELECTION_FIELDS=('min_x','min_y','min_z','max_x','max_y','max_z','excluded',
                  'extra_exclusions','foreground','regions','region_name')


def selection_snapshot(settings):
    return dict({key:getattr(settings,key) for key in SELECTION_FIELDS},_selected=state(settings.id_data).has_selection)


def restore_selection(settings,snapshot):
    for key,value in snapshot.items():
        if key=='_selected':state(settings.id_data).has_selection=value
        else:setattr(settings,key,value)


def remember_selection(s,snapshot):
    if not s.history or s.history[-1]!=snapshot:s.history.append(snapshot)
    del s.history[:-40]
    s.future.clear()


def action_locked(s,cmd):
    from . import world_exporter as ui
    if ui.busy() and ui._job.get('action')=='WORLDS' and not s.pending:
        if cmd in {('menu','worlds'),('browse','world_path'),('search_focus',)} or cmd[0]=='world':return False
    busy=s.importing or ui.foreground_busy() or bool(s.pending)
    if s.importing and cmd[0]=='cancel':return True
    return busy and cmd[0] not in {'cancel','section','line','inspector','zoom','pack_search','pack_resize','pack_group',
                                   'zoom_reset','fit','fit_world','log','close_menu','layout',
                                   'preview','view','preview_reset','preview_reload'}


_JSON_CACHE={}


def cached_json(s,text):
    """Selection lists are JSON strings on the settings; parse each value once."""
    cache=getattr(s,'json_cache',None) if s is not None else _JSON_CACHE
    if cache is None:
        cache=s.json_cache={}
    value=cache.get(text)
    if value is None:
        if len(cache)>32:cache.clear()
        value=cache[text]=json.loads(text)
    return value


def export_reason(settings,s):
    if not s.loaded:return 'Load a world to begin'
    if not s.ui_ready:return 'Wait for the map to load'
    if not s.has_selection:return 'Select an area on the map'
    for axis in 'xyz':
        if getattr(settings,'min_'+axis)>getattr(settings,'max_'+axis):return f'Minimum {axis.upper()} must not exceed maximum {axis.upper()}'
    if not settings.region_name.strip():return 'Name this area under More options, Areas & presets'
    regions=cached_json(s,settings.regions)
    names=[settings.region_name]+[r.get('name','') for r in regions]
    if len(set(names))!=len(names):return 'Give each saved area a different name'
    if any(r['min'+a]>r['max'+a] for r in regions for a in 'XYZ'):return 'A saved area has a min above its max'
    return ''


ICONS=Path(__file__).resolve().parents[1]/'assets'/'ui'/'icons'
# Sparkle roles: the one primary action is Sparkle Blue with a near-white
# label; every other control sits on the #242424 raised grey.
PRIMARY=T.accent
# Pressing deepens the accent instead of washing it out, keeping white labels legible.
PRIMARY_HOVER=T.accent_pressed
DANGER_FILL=(199/255,41/255,46/255,1.)
FOCUS=T.text
SELECTED_TEXT=T.accent_hover
SOFT_TEXT=T.text_muted
TRACK=T.surface
TRACK_EDGE=T.border
HAIRLINE=T.border
# A value field being edited shifts to the selected wash with a blue border.
AXIS_FOCUS=T.selected
SWITCH_OFF=(48/255,48/255,48/255,1.)
# Text and icons on an active map tool, which sits on the selected wash.
TOOL_ACTIVE=T.accent_hover


def blend_color(a,b,t):
    return tuple(x+(y-x)*t for x,y in zip(a,b))


class Painter:
    icons=ICONS

    def __init__(self,r,s):
        self.r,self.s=r,s
        self.clip=None

    def ease(self,key,target,duration=.14):
        """Interruptible selection transitions; Reduce Motion snaps."""
        from .gpu_framework.motion import tween
        return tween(self.s.motion,'miex.'+key,float(target),duration)

    def hovered(self,rect):
        """The pointer is over this control (never while dragging)."""
        if self.s.drag:return False
        hit=rect.intersect(self.clip) if self.clip else rect
        return hit.width>0 and hit.height>0 and hit.contains(*self.s.hover)

    def lift(self,rect,action,enabled=True):
        """Eased 0..1 hover of a button or segmented option; an open menu takes it."""
        return self.ease('hover.'+repr(action),enabled and not self.s.menu and self.hovered(rect),.12)

    def wash(self,rect,action,enabled=True,radius=CONTROL_RADIUS-1):
        """The lift behind an unselected option under the pointer; returns its amount."""
        lift=self.lift(rect,action,enabled)
        if lift>.01:self.r.rect(rect,T.surface_lift[:3]+(lift,),radius=radius)
        return lift

    def pressed(self,action):
        """Short tactile darkening after a click, independent of hover."""
        if self.s.motion.get('_motion_disabled'):return 0.
        started=self.s.flash.get(repr(action))
        if started is None:return 0.
        progress=(self.s.motion.get('_motion_time',time.monotonic())-started)/.16
        if progress>=1 or progress<0:return 0.
        self.s.motion['_motion_active']=True
        return 1-progress

    def focused(self,action):
        """Keyboard focus rings only; a click never leaves a ring behind."""
        return self.s.focus==action and getattr(self.s,'focus_visible',False)

    def ring(self,rect,action,radius=CONTROL_RADIUS):
        """Focus ring drawn just outside the control, visible on accent fills too."""
        if self.focused(action):
            self.r.rect(Rect(rect.x-2,rect.y-2,rect.width+4,rect.height+4),(0,0,0,0),radius=radius+2,border=FOCUS,border_width=1.5)

    def tip(self,rect,text):
        """Hover tooltip for canvas controls; drawn by draw_tooltip at the end of the frame."""
        if text:
            hit=rect.intersect(self.clip) if self.clip else rect
            if hit.width>0 and hit.height>0:self.s.tips.append((hit,text))

    def target(self,rect,action):
        hit=rect.intersect(self.clip) if self.clip else rect
        if hit.height>0 and hit.width>0:self.s.targets.append((hit,action))

    def icon(self,rect,name,color,size=16,rotation=0.):
        box=Rect(rect.x+(rect.width-size)/2,rect.y+(rect.height-size)/2,size,size)
        if rotation:self.r.image(box,str(ICONS/(name+'.png')),tint=color,rotation=rotation)
        else:self.r.image(box,str(ICONS/(name+'.png')),tint=color)

    def icon_button(self,rect,icon,action,tip=''):
        enabled=not action_locked(self.s,action)
        self.wash(rect,action,enabled,CONTROL_RADIUS)
        self.icon(rect,icon,T.text if enabled else T.text_muted,12)
        self.ring(rect,action)
        if enabled:self.target(rect,action);self.tip(rect,tip)

    def text(self,rect,text,muted=False,size=13,weight='Regular'):
        self.r.text(rect,text,T.text_muted if muted else T.text,size=size,weight=weight)

    def button(self,rect,text,action,active=False,enabled=True,align='CENTER',radius=CONTROL_RADIUS,surface=None,size=12,icon=None,chevron=False,weight='Regular',pad=10):
        """Sparkle buttons: the Sparkle Blue primary, #242424 secondary, near-white labels."""
        enabled=enabled and not action_locked(self.s,action)
        base=surface if surface is not None else T.surface_raised
        fill=PRIMARY if active and enabled else base
        # A disabled primary keeps its place but drops to the raised grey with a muted label.
        if active and not enabled:fill=base
        press=self.pressed(action) if enabled else 0.
        lift=self.lift(rect,action,enabled)
        if lift and not press:fill=blend_color(fill,T.accent_hover if active else T.surface_lift,lift)
        if press:fill=blend_color(fill,T.accent_pressed if active else T.surface_pressed,press*.6)
        self.r.rect(rect,fill,radius=radius)
        if enabled:self.ring(rect,action,radius)
        color=T.selected_text if active and enabled else T.text if enabled else T.text_muted
        if icon:self.icon(Rect(rect.x+8,rect.y,16,rect.height),icon,color)
        if chevron:self.icon(Rect(rect.right-22,rect.y,12,rect.height),'chevron',color,12)
        self.r.text(rect.inset((30 if icon and text else pad,0,26 if chevron else pad,0)),text,color,size=size,align=align,weight=weight)
        if enabled:self.target(rect,action)

    def axis(self,rect,letter,settings,prop,auto=False):
        """Value field: #242424 box with a centred Medium value; an optional axis letter sits inside on the left."""
        action=('edit',prop)
        editing=self.s.edit is not None and self.s.edit[0]==prop
        enabled=not auto and not action_locked(self.s,action)
        focus=self.ease('value.focus.'+prop,editing,.12)
        self.r.rect(rect,blend_color(T.surface_raised,AXIS_FOCUS,focus),radius=4,border=T.accent if editing else None)
        self.ring(rect,action,4)
        inner=rect.inset((24 if letter else 8,0,8,0))
        align='RIGHT' if letter else 'CENTER'
        if letter:self.r.text(Rect(rect.x+9,rect.y,14,rect.height),letter,T.text_muted,size=11.5,weight='Medium')
        if auto:
            self.r.text(inner,'Auto',T.text_muted,size=12,align=align)
            self.tip(rect,'Centered on the area. Turn off Center at origin to set it.')
            return
        value=getattr(settings,prop)
        if editing:
            text=self.s.edit[1]
            if getattr(self.s,'select_all',False) and text:
                from .world_exporter_layout import text_width
                width=text_width(self.r,text,12)+4
                self.r.rect(Rect(inner.right-width+2,rect.y+4,width,rect.height-8),T.accent[:3]+(.45,),radius=2)
            text+='|'
        else:text=f'{value:,}' if isinstance(value,int) else str(value)
        self.r.text(inner,text,T.text if enabled else T.text_muted,size=12,weight='Medium',align=align)
        scrubbing=bool(self.s.drag and self.s.drag[0]=='number' and self.s.drag[1]['prop']==prop)
        if prop in {'min_y','max_y'} and (editing or scrubbing):
            # The top-down map shows the height range while a Y field is in use.
            self.s.y_hint_until=self.s.motion.get('_motion_time',time.monotonic())+.4
        if enabled:self.target(rect,action)

    def field(self,rect,label,settings,prop):
        """Label on the left, value box on the right, split 48/52."""
        self.r.text(Rect(rect.x,rect.y,rect.width*.46,rect.height),label,T.text,size=12)
        self.value(Rect(rect.x+rect.width*.48,rect.y,rect.width*.52,rect.height),settings,prop)

    def value(self,box,settings,prop):
        value=getattr(settings,prop)
        editing=self.s.edit is not None and self.s.edit[0]==prop
        # Empty overrides fall back to the bundled runtime; say so.
        if isinstance(value,str) and not editing:
            text=value or ('Bundled' if prop in {'java','jar'} else '')
            action=('edit',prop);enabled=not action_locked(self.s,action)
            self.r.rect(box,T.surface_raised,radius=4)
            self.ring(box,action,4)
            from .world_exporter_layout import fit_text
            shown=fit_text(self.r,text,box.width-16,12,left='/' in text or '\\' in text)
            self.r.text(box.inset((8,0,8,0)),shown,T.text if value and enabled else T.text_muted,size=12,weight='Medium',align='CENTER')
            if value:self.tip(box,value)
            if enabled:self.target(box,action)
            return
        self.axis(box,'',settings,prop)

    def switch(self,rect,action,on,key,enabled=True,hit=True):
        """Sidebar switch: 36 x 20, Sparkle Blue and a near-white thumb when on."""
        enabled=enabled and not action_locked(self.s,action)
        amount=self.ease('switch.'+key,bool(on),.18)
        track=blend_color(SWITCH_OFF,T.accent if enabled else T.text_muted,amount)
        knob=rect.height-6
        self.r.rect(rect,track,radius=rect.height/2)
        self.ring(rect,action,rect.height/2)
        self.r.rect(Rect(rect.x+3+(rect.width-rect.height)*amount,rect.y+3,knob,knob),
                    T.selected_text if enabled else T.text_muted,radius=knob/2)
        if enabled and hit:self.target(rect,action)

    def toggle(self,rect,label,settings,prop):
        """Label with the sidebar switch; the whole row flips it."""
        action=('toggle',prop)
        enabled=not action_locked(self.s,action)
        self.r.text(Rect(rect.x,rect.y,rect.width-48,rect.height),label,T.text if enabled else T.text_muted,size=12)
        self.switch(Rect(rect.right-36,rect.y+(rect.height-20)/2,36,20),action,getattr(settings,prop),'toggle.'+prop,enabled,hit=False)
        if enabled:self.target(rect,action)

    def segmented(self,rect,key,options,size=11.5,tabs=False):
        """Recessed #141414 track; the chosen option sits on a Sparkle Blue pill that slides over 0.22 s,
        like the dimension tabs."""
        count=max(1,len(options))
        self.r.rect(rect,T.canvas,radius=CONTROL_RADIUS)
        width=rect.width/count
        selected=next((i for i,(_,_,active) in enumerate(options) if active),-1)
        if selected>=0:
            position=self.ease('segment.'+key,selected,.22)
            self.r.rect(Rect(rect.x+position*width+2,rect.y+2,width-4,rect.height-4),T.accent,radius=CONTROL_RADIUS-1)
        for i,(label,action,active) in enumerate(options):
            cell=Rect(rect.x+i*width,rect.y,width,rect.height)
            enabled=not action_locked(self.s,action)
            lift=0. if active else self.wash(cell.inset((2,2,2,2)),action,enabled)
            color=((1,1,1,1) if active else blend_color(T.text_muted,T.text,lift)) if enabled else T.text_muted[:3]+(.6,)
            self.r.text(cell,label,color,size=size,weight='SemiBold' if active else 'Medium',align='CENTER')
            if self.focused(action):self.r.rect(cell.inset((2,2,2,2)),(0,0,0,0),radius=CONTROL_RADIUS,border=FOCUS,border_width=1.5)
            if enabled:self.target(cell,action)

    def option(self,rect,title,subtitle,action,selected,enabled=True):
        """List choice: the chosen row takes the selected wash and a check, like a selected pack."""
        enabled=enabled and not action_locked(self.s,action)
        on=self.ease('option.'+repr(action),selected,.16)
        if on>.01:self.r.rect(rect,T.selected[:3]+(on,),radius=CONTROL_RADIUS)
        press=self.pressed(action) if enabled else 0.
        if press:self.r.rect(rect,T.surface_pressed[:3]+(press*.6,),radius=CONTROL_RADIUS)
        self.ring(rect,action)
        x=rect.x+10;width=rect.width-44
        color=T.text if enabled else T.text_muted
        weight='SemiBold' if selected else 'Medium'
        if subtitle:
            self.r.text(Rect(x,rect.top-25,width,18),title,color,size=12,weight=weight)
            self.r.text(Rect(x,rect.y+5,width,16),subtitle,T.text_muted,size=10.5)
        else:self.r.text(Rect(x,rect.y,width,rect.height),title,color,size=12,weight=weight)
        if on>.01:self.icon(Rect(rect.right-26,rect.y,14,rect.height),'check',T.accent_hover[:3]+(on,),12)
        if enabled:self.target(rect,action)

    def card(self,rect,name,subtitle,source,action,kind='pack'):
        """Saved preset row: bookmark icon, name and the date it was saved."""
        from .world_exporter_layout import fit_text,text_width
        enabled=not action_locked(self.s,action)
        press=self.pressed(action) if enabled else 0.
        if press:self.r.rect(rect,T.surface_pressed[:3]+(press*.6,),radius=CONTROL_RADIUS)
        self.ring(rect,action)
        self.icon(Rect(rect.x+8,rect.y,16,rect.height),'bookmark-simple',T.text_muted,14)
        room=max(0,rect.width-34-text_width(self.r,subtitle,10.5)-18)
        self.r.text(Rect(rect.x+30,rect.y,room,rect.height),fit_text(self.r,name,room,12,'Medium'),
                    T.text if enabled else T.text_muted,size=12,weight='Medium')
        self.r.text(Rect(rect.x,rect.y,rect.width-8,rect.height),subtitle,T.text_muted,size=10.5,align='RIGHT')
        self.tip(rect,'Load this preset')
        if enabled:self.target(rect,action)

    def hairline(self,x,y,width):
        self.r.rect(Rect(x,y,width,1),HAIRLINE)

    def fade(self,rect,color,downward=True,strength=1.):
        """Eased-gradient scrim with ease-out stops instead of a hard edge."""
        steps=12
        band=rect.height/steps
        for i in range(steps):
            alpha=(1-i/steps)**2.2*strength
            y=rect.top-(i+1)*band if downward else rect.y+i*band
            self.r.rect(Rect(rect.x,y,rect.width,band+.5),color[:3]+(color[3]*alpha,))


def draw_canvas():
    context=bpy.context
    if not _enabled or not context.workspace or not context.workspace.get(KEY) or context.area.type!='IMAGE_EDITOR':return
    # Render windows opened from this tab share its workspace; they show the render.
    if context.screen.is_temporary:return
    s=state(context.scene)
    use_area(s,context.area.as_pointer())
    s.hidden_since=None;s.released=False
    s.scale=max(.5,context.preferences.system.ui_scale)
    s.size=context.region.width/s.scale,context.region.height/s.scale
    if s.renderer is None or s.renderer.region!=context.region:
        s.renderer=BlenderRenderer(context.region,s.scale)
    r=s.renderer;r.ui_scale=s.scale;r.linear_output=True;r.cap_center_default=True
    r.begin()
    try:
        s.targets=[];s.tips=[]
        paint=Painter(r,s)
        from .world_exporter_view import render
        render(context,s,paint,draw_map)
        if s.menu:
            if s.menu != 'worlds':
                s.world_picker_visible = 0
                s.motion.get('_motion',{}).pop('worlds.reveal',None)
            draw_menu(context,paint,s)
        elif s.world_picker_visible > 0:
            from .world_exporter_worlds import draw_worlds
            draw_worlds(context,paint,s,closing=True)
        draw_tooltip(paint,s)
    finally:r.end()


def draw_tooltip(paint,s):
    """Blender-style tooltip after a short hover; no tooltip while dragging or in menus."""
    r=paint.r
    s.tip_shown=False
    tips=getattr(s,'tips',[])
    hit=next(((rect,text) for rect,text in reversed(tips) if rect.contains(*s.hover)),None)
    now=s.motion.get('_motion_time',time.monotonic())
    if hit is None or s.drag or s.menu:
        s.tip_text=None
        return
    if getattr(s,'tip_text',None)!=hit[1]:
        s.tip_text=hit[1];s.tip_since=now
    waited=now-getattr(s,'tip_since',now)
    if waited<.55:
        s.motion['_motion_active']=True
        return
    from .world_exporter_layout import text_width
    w,h=s.size
    # A long tip (a full path) stays on screen; the renderer ends it with an ellipsis.
    width=min(text_width(r,hit[1],12)+20,max(40,w-16))
    box=Rect(min(max(8,s.hover[0]-width/2),w-width-8),hit[0].top+8,width,24)
    if box.top>h-8:box=Rect(box.x,hit[0].y-32,width,24)
    r.rect(box,T.surface_raised,radius=CONTROL_RADIUS,border=T.border)
    r.text(box.inset((6,0,6,0)),hit[1],T.text,size=12,align='CENTER')
    s.tip_shown=True


def hover_redraw(s):
    """Passive motion redraws only when the control under the pointer changes
    (buttons and options lift) or a tooltip changes or shows (it follows the pointer)."""
    control=next((action for rect,action in reversed(s.targets) if rect.contains(*s.hover)),None)
    moved_on=control!=getattr(s,'hover_target',None)
    s.hover_target=control
    if s.menu:return moved_on
    under=next((text for rect,text in reversed(s.tips) if rect.contains(*s.hover)),None)
    return moved_on or under!=s.tip_text or (under is not None and s.tip_shown)


def leave_hover(s):
    """The pointer left the canvas: drop its tooltip. True when one was up."""
    up=s.tip_text is not None
    s.hover=(-1e9,-1e9);s.tip_text=None;s.tip_shown=False
    return up


def draw_map(context,s,paint,settings,rect):
    # The 3D preview takes the map's place while it is on or fading out.
    if s.preview.on or s.preview.shown>0 or s.preview.scene is not None:
        from .world_exporter_preview import draw
        draw(context,s,paint,settings,rect,lambda:draw_flat_map(context,s,paint,settings,rect))
        return
    draw_flat_map(context,s,paint,settings,rect)


def draw_flat_map(context,s,paint,settings,rect):
    r=paint.r
    r.push_clip(rect)
    from .world_exporter_layout import MAP_GROUND
    r.rect(rect,MAP_GROUND)
    viewrect=(rect.x,rect.y,rect.width,rect.height)
    if s.loaded:world_map_renderer.draw_pages(r,s,rect,settings if s.has_selection else None)
    if s.loaded and (s.snap or s.mode in {'CHUNK','EXCLUDE','FOREGROUND'}) and s.view.scale>=2:
        draw_chunk_grid(r,s,rect,viewrect)
    if s.loaded and s.textures and s.has_selection:
        for other in cached_json(s,settings.regions):
            ox,oy=s.view.screen(other['minX'],other['maxZ']+1,viewrect)
            r.rect(Rect(ox,oy,(other['maxX']-other['minX']+1)*s.view.scale,(other['maxZ']-other['minZ']+1)*s.view.scale),
                   (.25,.8,.5,.15),border=T.success)
        # Typed bounds can be inverted; draw the real area and flag it instead of a negative box.
        x0,x1=sorted((settings.min_x,settings.max_x));z0,z1=sorted((settings.min_z,settings.max_z))
        inverted=settings.min_x>settings.max_x or settings.min_z>settings.max_z
        x,y=s.view.screen(x0,z1+1,viewrect)
        selection=Rect(x,y,(x1-x0+1)*s.view.scale,(z1-z0+1)*s.view.scale)
        r.rect(selection,T.danger[:3]+(.06,) if inverted else T.accent[:3]+(.06,),border=T.danger if inverted else T.accent,border_width=2)
        for px,py in ((selection.x,selection.y),(selection.right,selection.y),(selection.x,selection.top),(selection.right,selection.top),
                      (selection.x+selection.width/2,selection.y),(selection.x+selection.width/2,selection.top),
                      (selection.x,selection.y+selection.height/2),(selection.right,selection.y+selection.height/2)):
            r.rect(Rect(px-4,py-4,8,8),T.text,border=T.accent)
        boxes=[];marks=[]
        for cx,cz in cached_json(s,settings.excluded):
            px,py=s.view.screen(cx*16,(cz+1)*16,viewrect)
            box=Rect(px,py,16*s.view.scale,16*s.view.scale)
            hit=box.intersect(rect)
            if hit.width and hit.height:
                boxes.append(box)
                if box.width>14:
                    size=min(16,box.width*.6)
                    marks.append(Rect(box.x+(box.width-size)/2,box.y+(box.height-size)/2,size,size))
        # Many excluded chunks go out in two batched draws rather than two calls each.
        if len(boxes)>32 and world_map_renderer.batched(r):
            world_map_renderer.marks(r,boxes,T.danger[:3]+(.4,),T.danger,marks,str(ICONS/'close.png'),T.text)
        else:
            for box in boxes:r.rect(box,T.danger[:3]+(.4,),border=T.danger)
            for mark in marks:r.image(mark,str(ICONS/'close.png'),tint=T.text)
        if s.drag and s.drag[0]=='select' and s.mode=='EXCLUDE':
            x0,z0,x1,z1=drag_bounds(s.drag[2],s.view.world(*s.hover,viewrect),True)
            px,py=s.view.screen(x0,z1+1,viewrect)
            r.rect(Rect(px,py,(x1-x0+1)*s.view.scale,(z1-z0+1)*s.view.scale),T.danger[:3]+(.18,),border=T.danger,border_width=2)
        for fg in cached_json(s,settings.foreground):
            parts=fg.split('_')
            if len(parts)==3 and parts[0]=='chunk':
                try:
                    ix,iz=map(int,parts[1:])
                    cx=settings.min_x//16+(ix-1)*settings.chunk_size
                    cz=settings.min_z//16+(iz-1)*settings.chunk_size
                    px,py=s.view.screen(cx*16,(cz+settings.chunk_size)*16,viewrect)
                    r.rect(Rect(px,py,16*settings.chunk_size*s.view.scale,16*settings.chunk_size*s.view.scale),(1,.7,.1,.15),border=(1,.7,.1,.9))
                except ValueError:pass
        if s.drag and s.drag[0] in {'select','resize','edge_x','edge_z','move'} and s.mode in {'BOX','CHUNK'}:
            draw_guides(r,selection,rect)
        draw_size_label(paint,settings,s,selection,rect)
    # A map that is still loading stays quiet; the loading card speaks for it.
    if s.loaded and s.ui_ready and not (s.view.page_keys(viewrect) & s.map_pages) and (s.map_pages or s.failed or s.paused):
        message = ('The map could not load. Choose Reload map in the menu.' if s.failed else
            'Map paused. Resume it from the menu.' if s.paused and not s.map_pages else 'No generated chunks in this area')
        paint.text(Rect(rect.x+20,rect.y+rect.height/2,rect.width-40,30),message,True)
    r.pop_clip()


def draw_chunk_grid(r,s,rect,viewrect):
    """Chunk lines, one rect per line instead of one per chunk; follows negative coordinates."""
    color=(.5,.6,.65,.16)
    left,top=s.view.world(rect.x,rect.top,viewrect)
    right,bottom=s.view.world(rect.right,rect.y,viewrect)
    step=16*s.view.scale
    first_x=math.floor(min(left,right)/16)*16
    for index in range(int(abs(right-left)/16)+2):
        x=s.view.screen(first_x+index*16,0,viewrect)[0]
        if rect.x<=x<=rect.right:r.rect(Rect(x,rect.y,1,rect.height),color)
    first_z=math.floor(min(top,bottom)/16)*16
    for index in range(int(abs(bottom-top)/16)+2):
        y=s.view.screen(0,first_z+index*16,viewrect)[1]
        if rect.y<=y<=rect.top:r.rect(Rect(rect.x,y,rect.width,1),color)


def draw_guides(r,selection,rect):
    """Dashed alignment guides from the selection edges to the map edges while dragging."""
    color=T.accent[:3]+(.5,)
    dash,gap=6,5
    if world_map_renderer.batched(r):
        # One draw per guide; dash by dash was hundreds of draw calls a frame.
        for y in (selection.y,selection.top):
            for x0,x1 in ((rect.x,selection.x),(selection.right,rect.right)):
                if x1>x0:world_map_renderer.dashes(r,Rect(x0,y-.5,x1-x0,1),color,dash,gap)
        for x in (selection.x,selection.right):
            for y0,y1 in ((rect.y,selection.y),(selection.top,rect.top)):
                if y1>y0:world_map_renderer.dashes(r,Rect(x-.5,y0,1,y1-y0),color,dash,gap,vertical=True)
        return
    for y in (selection.y,selection.top):
        for x0,x1 in ((rect.x,selection.x),(selection.right,rect.right)):
            x=x0
            while x<x1:
                r.rect(Rect(x,y-.5,min(dash,x1-x),1),color)
                x+=dash+gap
    for x in (selection.x,selection.right):
        for y0,y1 in ((rect.y,selection.y),(selection.top,rect.top)):
            y=y0
            while y<y1:
                r.rect(Rect(x-.5,y,1,min(dash,y1-y)),color)
                y+=dash+gap


def selection_chunks(settings):
    chunks=(settings.max_x//16-settings.min_x//16+1)*(settings.max_z//16-settings.min_z//16+1)
    excluded=sum(settings.min_x//16<=p[0]<=settings.max_x//16 and settings.min_z//16<=p[1]<=settings.max_z//16
                 for p in cached_json(None,settings.excluded))
    return max(0,chunks-excluded)


def draw_size_label(paint,settings,s,selection,rect):
    """Selection size as plain white text riding above its top-left corner, live while dragging."""
    r=paint.r
    width=settings.max_x-settings.min_x+1;depth=settings.max_z-settings.min_z+1;height=settings.max_y-settings.min_y+1
    flipped=next((axis.upper() for axis in 'xyz' if getattr(settings,'min_'+axis)>getattr(settings,'max_'+axis)),None)
    height_hint=s.motion.get('_motion_time',time.monotonic())<getattr(s,'y_hint_until',0.)
    if height_hint:s.motion['_motion_active']=True
    narrowed=(settings.min_y,settings.max_y)!=(-64,319)
    text=(f'Min {flipped} is greater than max {flipped}. Swap the values.' if flipped else
          f'Y {settings.min_y} to {settings.max_y} · {height:,} blocks tall' if height_hint else
          f'{width:,} × {depth:,} × {height:,} blocks' if narrowed else
          f'{width:,} × {depth:,} blocks')
    measured=r.text_width(text,12,'SemiBold') if hasattr(r,'text_width') else None
    label_width=(measured if isinstance(measured,(int,float)) else len(text)*6.6)+(16 if flipped else 2)
    label_height=20 if flipped else 15
    # 7 px above the outline; inside the top edge when the map has no room above.
    y=selection.top+7
    if y+label_height>rect.top-8:y=selection.top-7-label_height
    label=Rect(max(rect.x+8,min(selection.x,rect.right-label_width-8)),max(rect.y+8,min(y,rect.top-label_height-8)),
               label_width,label_height)
    if flipped:
        # A deeper red keeps the white warning text readable (4.9:1).
        r.rect(label,DANGER_FILL,radius=CONTROL_RADIUS)
        r.text(label,text,T.selected_text,size=12,weight='SemiBold',align='CENTER')
        return
    # A soft shadow keeps the white label legible over snow and sand.
    r.text(Rect(label.x+1,label.y-1,label.width,label.height),text,(0,0,0,.45),size=12,weight='SemiBold')
    r.text(label,text,(1,1,1,1),size=12,weight='SemiBold')


# The world picker is the one searchable menu; the rest drop from a control
# or open at the pointer.
SEARCH_MENUS={'worlds'}


def dimension_label(name):
    return name.removeprefix('minecraft:').removeprefix('minecraft/').replace('_',' ').title()


def menu_items(context,s):
    """(label, command[, extras]) rows; extras may hold checked, meta and divider."""
    settings=context.scene.mcbedrock_world_exporter
    if s.menu=='water styles':
        return [(item.name,('water_preset',item.identifier))
                for item in settings.bl_rna.properties['water_preset'].enum_items]
    if s.menu=='pack actions':
        from .pack_manager import is_favorite
        packs=context.scene.mcbedrock_active_packs
        index=next((i for i,p in enumerate(packs) if p.path==getattr(s,'context_pack',None)),-1)
        key=getattr(s,'context_pack_key',None)
        rows=[]
        if index>=0:
            rows=[(label,('stack',s.context_pack,target)) for label,target in
                  [('Move up',index-1),('Move down',index+1),('Move to top',0)] if 0<=target<len(packs) and target!=index]+[('Remove from active stack',('stack_remove',s.context_pack))]
        if key:
            favorite=is_favorite(key)
            rows.append(('Remove from favorites' if favorite else 'Add to favorites',('pack_favorite',key,not favorite),
                         dict(divider=True) if rows else {}))
        return rows
    if s.menu=='preview actions':
        from .world_exporter_preview import menu_items as preview_items
        return preview_items(s)
    if s.menu in {'map actions','selection actions'}:
        items=[]
        if s.menu=='selection actions':
            items=[('Fit area',('fit',)),('Exclude chunks…',('mode','EXCLUDE')),('Edit foreground groups…',('mode','FOREGROUND')),
                   ('Save preset',('save_preset',)),('Clear area',('clear_selection',))]
        items += [('Go to player…',('menu','players')),('Fit world',('fit_world',)),
                  ('Stop snapping to chunks' if s.snap else 'Snap to chunks',('snap',))]
        if s.history:items.append(('Undo area change (Ctrl Z)',('selection_undo',)))
        if s.future:items.append(('Redo area change (Ctrl ⇧ Z)',('selection_redo',)))
        items += [('Hide panel (N)' if s.inspector_open else 'Show panel (N)',('inspector',)),('Reload map',('reload',)),
                  ('Resume map' if s.paused else 'Pause map',('pause',)),('Back to Layout',('layout',),{'divider':True})]
        return items
    if s.menu=='worlds':
        return [(' '.join((w.display_name or w.name, w.edition, w.version, w.launcher, w.instance)),('world',i)) for i,w in enumerate(settings.worlds)]
    if s.menu=='dimensions':
        return [(dimension_label(d.name),('dimension',d.name),{'checked':d.name==settings.dimension}) for d in settings.dimensions]
    if s.menu=='players':return [(p.name,('player',i)) for i,p in enumerate(settings.players)]
    if s.menu=='regions':
        return [('Delete this area',('delete_region',))]+[(p.get('name','Area'),('region',i),{'divider':i==0}) for i,p in enumerate(cached_json(s,settings.regions))]
    return []


def menu_hint(context,s):
    """A muted closing line under some menus, or the reason a menu is empty."""
    if s.menu=='dimensions' and not len(context.scene.mcbedrock_world_exporter.dimensions):return 'Load a world to list its dimensions'
    if s.menu=='players' and not len(context.scene.mcbedrock_world_exporter.players):return 'This world has no saved players'
    return ''


def draw_menu(context,paint,s):
    if s.menu=='worlds':
        from .world_exporter_worlds import draw_worlds
        draw_worlds(context, paint, s)
        return
    # Menus drop 6 px into place while fading in, so each opening reads as coming from its control.
    from .gpu_framework.motion import tween
    if getattr(s,'menu_shown',None)!=s.menu:
        s.menu_shown=s.menu
        s.motion.get('_motion',{}).pop('miex.menu.open',None)
    shown=tween(s.motion,'miex.menu.open',1.,.16,initial=0.)
    r=paint.r
    opacity,offset=r.opacity,getattr(r,'offset_y',0.)
    r.opacity=opacity*shown;r.offset_y=offset+6*(1-shown)
    try:_draw_menu(context,paint,s)
    finally:r.opacity,r.offset_y=opacity,offset


def _draw_menu(context,paint,s):
    from .world_exporter_layout import text_width
    r=paint.r
    w,h=s.size
    searchable=s.menu in SEARCH_MENUS
    items=[item for item in menu_items(context,s) if s.search.lower() in item[0].lower()]
    hint=menu_hint(context,s)
    row=28
    labels=max([text_width(r,item[0],13)+(text_width(r,item[2].get('meta',''),12)+16 if len(item)>2 else 0)
                for item in items]+[text_width(r,hint,12)])
    width=min(360 if searchable else max(200,labels+48),w-24)
    header=76 if searchable else 0
    footer=26 if hint else 0
    count=max(1,min(len(items),int((h-40-header-footer-10)/row)))
    dividers=sum(1 for i,item in enumerate(items[:count]) if i and len(item)>2 and item[2].get('divider'))
    height=header+(count*row if items else row)+dividers*9+10+footer
    anchor=s.menu_anchor
    box=Rect(max(12,min(anchor[0],w-width-12)),max(12,min(anchor[1]-height,h-height-12)),width,height)
    s.menu_rect=box
    # Sparkle menu: #242424 with 8 px corners and the one soft shadow in the system.
    for spread,alpha in ((6,.10),(3,.16)):
        r.rect(Rect(box.x-spread,box.y-spread-2,box.width+2*spread,box.height+2*spread),(0,0,0,alpha),radius=8+spread)
    r.rect(box,T.surface_raised,radius=8,border=T.border)
    # Modal targets replace underlying controls, preventing click-through.
    s.targets=[]
    if searchable:
        title=s.menu.capitalize()
        r.text(Rect(box.x+14,box.top-38,width-60,26),title,T.text,size=12.5,weight='SemiBold')
        paint.icon_button(Rect(box.right-36,box.top-36,24,24),'close',('close_menu',),'Close')
        search=Rect(box.x+10,box.top-72,width-20,28)
        r.rect(search,T.canvas,radius=4)
        r.image(Rect(search.x+9,search.y+8,12,12),str(ICONS/'search.png'),tint=T.text_muted)
        r.text(Rect(search.x+28,search.y,search.width-36,search.height),(s.search+'|') if s.search else 'Type to search',
               T.text if s.search else T.text_muted,size=12)
    s.menu_scroll=min(s.menu_scroll,max(0,len(items)-count))
    s.menu_index=max(0,min(s.menu_index,len(items)-1))
    y=box.top-5-header
    for i,item in enumerate(items[s.menu_scroll:s.menu_scroll+count]):
        label,cmd=item[0],item[1]
        extra=item[2] if len(item)>2 else {}
        if extra.get('divider') and i:
            r.rect(Rect(box.x+5,y-5,box.width-10,1),T.border);y-=9
        cell=Rect(box.x+5,y-row,box.width-10,row)
        highlighted=s.worlds_keyboard_focus and s.menu_index==s.menu_scroll+i
        enabled=not action_locked(s,cmd)
        if highlighted:r.rect(cell,T.selected,radius=4)
        elif enabled and not s.drag and cell.contains(*s.hover):r.rect(cell,T.surface_lift,radius=4)
        if extra.get('checked'):paint.icon(Rect(cell.x+4,cell.y,16,row),'check',T.accent_hover,12)
        color=T.text if enabled else T.text_muted
        r.text(Rect(cell.x+26,cell.y,cell.width-34,row),label,color,size=12)
        if extra.get('meta'):r.text(Rect(cell.x,cell.y,cell.width-8,row),extra['meta'],T.text_muted,size=11,align='RIGHT')
        if enabled:s.targets.append((cell,cmd))
        y-=row
    if not items and not hint:
        r.text(Rect(box.x+14,y-row,width-28,row),'No matching items',T.text_muted,size=12);y-=row
    if hint:
        if items:r.rect(Rect(box.x+5,y-5,box.width-10,1),T.border)
        r.text(Rect(box.x+13,box.y+4,width-26,22),hint,T.text_muted,size=12)
    if len(items)>count:
        thumb=max(16,(height-header-footer-10)*count/len(items))
        track=height-header-footer-10
        r.rect(Rect(box.right-5,box.top-5-header-thumb-(track-thumb)*s.menu_scroll/max(1,len(items)-count),2,thumb),T.text_muted,radius=1)


def parse_number(text):
    """Whole numbers as people type them, including the '1,500' the fields display."""
    return int(round(float(str(text).replace(',','').replace('_','').strip())))


def commit(context,s):
    """Apply the value being typed; False, with a message, when it is not a number."""
    if not s.edit:return True
    prop,value=s.edit
    settings=context.scene.mcbedrock_world_exporter
    current=getattr(settings,prop)
    try:
        if isinstance(current,int):value=parse_number(value)
        before=selection_snapshot(settings) if prop in SELECTION_FIELDS and value!=current else None
        setattr(settings,prop,value)
        if before is not None:
            s.has_selection=True
            remember_selection(s,before)
        s.edit=None
        return True
    except (ValueError,TypeError,OverflowError):
        settings.status='Enter a valid number for '+prop.replace('_',' ')
        tell(s,settings.status,True)
        return False


def settle(context,s):
    """Moving on from a field applies it, or drops text that is not a number and says so,
    instead of leaving every other control dead until Esc."""
    if commit(context,s):return
    prop,text=s.edit;s.edit=None
    value=getattr(context.scene.mcbedrock_world_exporter,prop)
    tell(s,f'"{text}" is not a number; {prop.replace("_"," ")} stays {value:,}',True)


def action(context,s,cmd):
    from . import world_exporter as ui
    settings=context.scene.mcbedrock_world_exporter
    kind=cmd[0]
    if action_locked(s,cmd):return
    tell(s,'')
    if kind!='edit':settle(context,s)
    if kind!='pack_search':s.pack_search_focused=False
    # Dropdown and context menus close on the choice they offered.
    if s.menu and s.menu not in SEARCH_MENUS and kind not in {'menu','close_menu','search_focus'}:s.menu=None
    if kind in {'edit','toggle','engine_set','clear_selection','reset_selection'} and settings.status.startswith(('Imported','Export saved')):
        settings.status='Selection ready'
    if kind=='edit':
        settle(context,s)
        prop=cmd[1]
        if settings.bl_rna.properties[prop].type=='INT':
            s.edit=[prop,str(getattr(settings,prop))]
            s.select_all=True
            s.focus=cmd
        else:bpy.ops.mcbedrock.world_edit('INVOKE_DEFAULT',field=prop)
    elif kind=='inspector':s.inspector_open=not s.inspector_open
    elif kind=='preview':
        from .world_exporter_preview import toggle
        toggle(s)
    elif kind=='view':
        from .world_exporter_preview import set_view
        set_view(s,cmd[1])
    elif kind=='preview_reset':s.preview.reset_view()
    elif kind=='preview_reload':s.preview.reload()
    elif kind=='section':
        # Sections open and close independently.
        if cmd[1] in s.sections:s.sections.remove(cmd[1])
        else:s.sections.add(cmd[1])
    elif kind=='snap':
        s.snap=not(s.snap or s.mode=='CHUNK')
        if s.mode=='CHUNK':s.mode='BOX'
    elif kind=='selection_undo':
        if s.history:
            s.future.append(selection_snapshot(settings))
            restore_selection(settings,s.history.pop())
    elif kind=='selection_redo':
        if s.future:
            s.history.append(selection_snapshot(settings))
            restore_selection(settings,s.future.pop())
    elif kind=='toggle':setattr(settings,cmd[1],not getattr(settings,cmd[1]))
    elif kind=='mode':
        # Exclude and foreground editing act on an existing area.
        if cmd[1] in {'EXCLUDE','FOREGROUND'} and not s.has_selection:return
        s.mode=cmd[1]
    elif kind=='zoom':s.view.zoom(cmd[1],s.map_rect.x+s.map_rect.width/2,s.map_rect.y+s.map_rect.height/2,tuple_rect(s.map_rect))
    elif kind=='zoom_reset':s.view.scale=1
    elif kind=='fit_world':
        bounds=s.world_bounds
        if bounds is None:
            chunks=[p for p,tile in s.tiles.items() if tile]
            if chunks:bounds=(min(p[0] for p in chunks)*16,min(p[1] for p in chunks)*16,max(p[0] for p in chunks)*16+15,max(p[1] for p in chunks)*16+15)
        if bounds:s.view.fit(bounds,tuple_rect(s.map_rect))
    elif kind=='fit':s.view.fit((settings.min_x,settings.min_z,settings.max_x,settings.max_z),tuple_rect(s.map_rect))
    elif kind=='menu':
        s.menu=cmd[1];s.search='';s.menu_scroll=0;s.menu_index=0
        # Toolbar and panel menus drop from their control; others open at the pointer.
        s.menu_anchor=getattr(s,'anchors',{}).get(cmd[1],s.hover)
        s.worlds_keyboard_focus=False
        s.search_focused=False;s.search_select_all=False
        if s.menu=='worlds':
            s.worlds_keyboard_focus=False
            s.worlds_refresh_started=time.monotonic()
            loaded_path=s.signature[0] if s.loaded and s.signature else settings.world_path if s.loaded else ''
            s.menu_index=next((i for i,w in enumerate(settings.worlds) if loaded_path and Path(w.path)==Path(loaded_path)),0)
            s.menu_scroll=s.menu_index
        if s.menu=='worlds' and not s.worlds_scanned:
            s.pending='WORLDS';s.worlds_scanned=True
    elif kind=='add_region':
        remember_selection(s,selection_snapshot(settings))
        regions=json.loads(settings.regions)
        regions.append(ui._snapshot(context)['region'])
        settings.regions=json.dumps(regions)
        i=1
        while 'Selection '+str(i) in [r.get('name') for r in regions]:i+=1
        settings.region_name='Selection '+str(i)
        settings.min_x+=16;settings.max_x+=16
        settings.excluded='[]';settings.extra_exclusions='[]';settings.foreground='[]'
    elif kind in {'region','delete_region'}:
        remember_selection(s,selection_snapshot(settings))
        from ..exporters.miex.presets import apply_region
        regions=json.loads(settings.regions)
        if regions:
            index=cmd[1] if kind=='region' else 0
            selected=regions.pop(index)
            if kind=='region':regions.append(ui._snapshot(context)['region'])
            settings.regions=json.dumps(regions)
            apply_region(settings,selected)
            action(context,s,('fit',))
        s.menu=None
    elif kind=='search_focus':s.search_focused=True;s.search_blink_started=time.monotonic()
    elif kind=='close_menu':s.menu=None;s.search_focused=False
    elif kind=='job':
        if cmd[1]=='EXPORT' and export_reason(settings,s):return
        s.pending=cmd[1]
        if cmd[1]!='WORLDS':s.menu=None
    elif kind=='cancel':
        loading=s.pending=='LOAD' or (ui.busy() and ui._job.get('action')=='LOAD') or (s.loaded and not s.ui_ready)
        s.pending=None
        if ui.busy():bpy.ops.mcbedrock.world_cancel()
        else:settings.status='Cancelled · selection kept'
        if loading:
            s.loaded=False;s.ui_ready=False;s.has_selection=False;s.paused=True
            invalidate(context.scene)
    elif kind=='world':settings.world_index=cmd[1];s.pending='LOAD';s.menu=None;s.has_selection=False
    elif kind=='dimension':
        s.menu=None
        # The open dimension's tab is already showing; only a change reloads.
        if cmd[1]!=settings.dimension:settings.dimension=cmd[1];s.pending='LOAD';s.has_selection=False
    elif kind=='player':
        p=settings.players[cmd[1]];settings.player=p.name
        s.view.x,s.view.z=p.x,p.z
        if p.dimension!=settings.dimension:settings.dimension=p.dimension;s.pending='LOAD'
        s.menu=None
    elif kind=='engine':settings.engine='EEVEE' if settings.engine=='CYCLES' else 'CYCLES'
    elif kind=='engine_set':settings.engine=cmd[1]
    elif kind=='whitewater_set':settings.whitewater_style=cmd[1]
    elif kind=='water_preset':settings.water_preset=cmd[1];s.menu=None
    elif kind in {'pack_toggle','pack_card'}:toggle_pack(context,cmd[1])
    elif kind=='pack_search':
        s.pack_search_focused=True;s.pack_search_select_all=False;s.pack_search_blink=time.monotonic()
    elif kind=='pack_group':
        s.pack_groups_closed^={cmd[1]}
    elif kind=='pack_folder':bpy.ops.mcbedrock.add_resource_pack_folder('INVOKE_DEFAULT')
    elif kind=='browse':
        s.menu=None
        bpy.ops.mcbedrock.world_browse('INVOKE_DEFAULT',target=cmd[1])
    elif kind=='scan':bpy.ops.mcbedrock.scan_resource_packs('EXEC_DEFAULT',background=True,prune=True)
    elif kind=='import':
        settings.status='Importing world into Blender…'
        ui._schedule_import(context.scene,context.window)
    elif kind=='frame_world':bpy.ops.mcbedrock.world_frame()
    elif kind in {'apply_water','restore_water'}:
        if bpy.ops.mcbedrock.world_water.poll():
            bpy.ops.mcbedrock.world_water(restore_original=kind=='restore_water')
    elif kind=='world_outlines':
        if bpy.ops.mcbedrock.world_outlines.poll():
            bpy.ops.mcbedrock.world_outlines(enable=cmd[1])
    elif kind=='water_color':
        bpy.ops.mcbedrock.plain_water_color('INVOKE_DEFAULT')
    elif kind=='world_materials':
        if bpy.ops.mcbedrock.world_materials.poll():
            bpy.ops.mcbedrock.world_materials(enable=cmd[1])
    elif kind=='check_world_scale':bpy.ops.mcbedrock.world_scale_check('INVOKE_DEFAULT')
    elif kind=='layout':
        workspace=next((w for w in bpy.data.workspaces if w.name=='Layout'),None)
        if workspace is None:workspace=next((w for w in bpy.data.workspaces if not w.get(KEY)),None)
        if workspace:context.window.workspace=workspace
    elif kind=='pack_favorite':bpy.ops.mcbedrock.favorite_pack('EXEC_DEFAULT',filepath=cmd[1],favorite=cmd[2])
    elif kind in {'stack','stack_remove'}:
        packs=context.scene.mcbedrock_active_packs
        index=next((i for i,p in enumerate(packs) if p.path==cmd[1]),-1)
        if index>=0:
            context.scene.mcbedrock_active_pack_index=index
            if kind=='stack':bpy.ops.mcbedrock.reorder_schematic_pack('EXEC_DEFAULT',pack_path=cmd[1],target_index=cmd[2])
            else:bpy.ops.mcbedrock.remove_active_pack('EXEC_DEFAULT')
    elif kind=='log':bpy.ops.mcbedrock.world_log()
    elif kind=='advanced':s.advanced=not s.advanced
    elif kind=='tools':s.tools=not s.tools
    elif kind=='pause':s.paused=not s.paused
    elif kind=='reload':invalidate(context.scene);s.pending='LOAD'
    elif kind=='reopen_world':s.pending='LOAD';s.menu=None
    elif kind=='output_mode':
        if cmd[1]=='BLEND':
            if bpy.data.filepath:settings.output=''
        else:settings.output='' if not bpy.data.filepath else str(ui.storage()/'exports')
    elif kind=='load_preset':
        s.preset_cache=None
        bpy.ops.mcbedrock.world_browse('EXEC_DEFAULT',target='preset',filepath=cmd[1])
    elif kind=='reset_selection':
        remember_selection(s,selection_snapshot(settings))
        s.has_selection=True
        settings.excluded='[]';settings.foreground='[]';settings.extra_exclusions='[]'
        # A 4 x 4 chunk area around the view center: big enough to see, quick to export.
        x,z=math.floor(s.view.x/16)*16,math.floor(s.view.z/16)*16
        settings.min_x,settings.max_x=x-32,x+31;settings.min_z,settings.max_z=z-32,z+31
    elif kind=='clear_selection':
        remember_selection(s,selection_snapshot(settings));s.has_selection=False;s.mode='BOX'
        settings.excluded=settings.foreground=settings.extra_exclusions='[]'
    elif kind=='save_preset':
        folder=ui.storage()/'presets';folder.mkdir(parents=True,exist_ok=True)
        name=''.join(c if c.isalnum() or c in '_- ' else '_' for c in settings.preset_name).strip() or 'Selection'
        path=folder/(name+'.json')
        replaced=path.exists()
        data={p:getattr(settings,p) for p in PRESET_FIELDS}
        data={p:(list(v) if hasattr(v,'__len__') and not isinstance(v,str) else v) for p,v in data.items()}
        from .world_exporter_packs import shared_stack
        data['packs']=[{'name':name,'path':path} for name,path in shared_stack(context.scene)]
        path.write_text(json.dumps(data,indent=2),encoding='utf-8')
        settings.status=('Preset updated: ' if replaced else 'Preset saved: ')+path.stem
        tell(s,settings.status)
        s.preset_cache=None


def tuple_rect(rect):return rect.x,rect.y,rect.width,rect.height


def toggle_pack(context,key):
    """Switch a pack on (bottom of the shared stack) or off, by its normalized source path."""
    from .pack_manager import _normalized_path
    from .world_exporter_packs import sync_mirror
    scene=context.scene
    active=next((i for i,p in enumerate(scene.mcbedrock_active_packs) if _normalized_path(p.source_path or p.path)==key),-1)
    if active>=0:
        scene.mcbedrock_active_pack_index=active
        bpy.ops.mcbedrock.remove_active_pack('EXEC_DEFAULT')
    else:
        index=next((i for i,p in enumerate(scene.mcbedrock_discovered_packs) if _normalized_path(p.path)==key),-1)
        if index<0:return
        bpy.ops.mcbedrock.add_resource_pack_to_active('EXEC_DEFAULT',pack_index=index)
    sync_mirror(scene)


def map_select(settings,s,start,end):
    if s.mode in {'BOX','CHUNK'}:
        s.has_selection=True
        if settings.status.startswith(('Imported','Export saved')):settings.status='Selection ready'
        settings.min_x,settings.min_z,settings.max_x,settings.max_z=drag_bounds(start,end,s.mode=='CHUNK' or s.snap)
    elif s.mode=='EXCLUDE':
        x0,z0,x1,z1=drag_bounds(start,end,True)
        excluded={tuple(p) for p in json.loads(settings.excluded)}
        for z in range(z0//16,z1//16+1):
            for x in range(x0//16,x1//16+1):
                point=(x,z)
                if point in excluded:excluded.remove(point)
                else:excluded.add(point)
        settings.excluded=json.dumps(sorted(excluded))
    elif s.mode=='FOREGROUND':
        if not(settings.min_x<=end[0]<settings.max_x+1 and settings.min_z<=end[1]<settings.max_z+1):return
        ix=(math.floor(end[0]/16)-settings.min_x//16)//settings.chunk_size+1
        iz=(math.floor(end[1]/16)-settings.min_z//16)//settings.chunk_size+1
        point=f'chunk_{ix}_{iz}'
        selected=set(json.loads(settings.foreground))
        if point in selected:selected.remove(point)
        else:selected.add(point)
        settings.foreground=json.dumps(sorted(selected))


def search_caret_visible(s,now=None):
    return s.menu=='worlds' and s.search_focused and int(((time.monotonic() if now is None else now)-s.search_blink_started)*2)%2==0


def press_control(context,s,cmd,x):
    """Defer numeric clicks until release so the same field can also scrub."""
    s.flash[repr(cmd)]=time.monotonic()
    settings=context.scene.mcbedrock_world_exporter
    prop=settings.bl_rna.properties.get(cmd[1]) if cmd[0]=='edit' else None
    if prop is None or prop.type!='INT':
        action(context,s,cmd);return
    if action_locked(s,cmd):return
    settle(context,s)
    value=getattr(settings,cmd[1])
    s.focus=cmd
    s.drag=('number',dict(prop=cmd[1],start=x,last=x,value=float(value),original=value,moved=False))
    s.drag_snapshot=selection_snapshot(settings) if cmd[1] in SELECTION_FIELDS else {cmd[1]:value}
    s.drag_button='LEFTMOUSE'


def cancel_number_drag(settings,s):
    if s.drag and s.drag[0]=='number':
        restore_selection(settings,s.drag_snapshot)
        s.drag=None;s.drag_snapshot=None;s.drag_button=None


def number_drag_event(context,s,event,x):
    settings=context.scene.mcbedrock_world_exporter
    drag=s.drag[1];prop=drag['prop']
    if action_locked(s,('edit',prop)) or event.type=='WINDOW_DEACTIVATE' or (event.type in {'ESC','RIGHTMOUSE'} and event.value=='PRESS'):
        cancel_number_drag(settings,s);return
    if event.type in {'MOUSEMOVE','INBETWEEN_MOUSEMOVE'}:
        if not drag['moved'] and abs(x-drag['start'])<4:return
        drag['moved']=True
        # Logical pixels keep sensitivity consistent with Blender's UI scale.
        # Accumulate deltas so changing Shift mid-drag never makes values jump.
        rate=.025 if event.shift else .25
        rna=settings.bl_rna.properties[prop]
        drag['value']=max(rna.hard_min,min(rna.hard_max,drag['value']+(x-drag['last'])*rate))
        drag['last']=x
        value=round(drag['value'])
        if value!=getattr(settings,prop):
            setattr(settings,prop,value)
            if prop in SELECTION_FIELDS:s.has_selection=True
            if settings.status.startswith(('Imported','Export saved')):settings.status='Selection ready'
    elif event.type=='LEFTMOUSE' and event.value=='RELEASE':
        if getattr(settings,prop)!=drag['original']:remember_selection(s,s.drag_snapshot)
        else:restore_selection(settings,s.drag_snapshot)
        s.drag=None;s.drag_snapshot=None;s.drag_button=None
        if not drag['moved']:action(context,s,('edit',prop))


def wheel_number(context,s,x,y,step):
    settings=context.scene.mcbedrock_world_exporter
    for hit,cmd in reversed(s.targets):
        if not hit.contains(x,y) or cmd[0]!='edit':continue
        prop=cmd[1]
        rna=settings.bl_rna.properties.get(prop)
        if rna is None or rna.type!='INT' or action_locked(s,cmd):return False
        settle(context,s)
        old=getattr(settings,prop)
        value=max(rna.hard_min,min(rna.hard_max,old+step))
        if value!=old:
            before=selection_snapshot(settings) if prop in SELECTION_FIELDS else None
            setattr(settings,prop,value)
            if before is not None:
                s.has_selection=True
                remember_selection(s,before)
            if settings.status.startswith(('Imported','Export saved')):settings.status='Selection ready'
        s.focus=cmd
        return True
    return False


def handle_event(context,s,event):
    """Shared by modal input and interaction regression tests."""
    settings=context.scene.mcbedrock_world_exporter
    x=(event.mouse_x-context.region.x)/s.scale;y=(event.mouse_y-context.region.y)/s.scale
    s.hover=x,y
    if s.native_edit:return
    if s.drag and s.drag[0]=='number':
        number_drag_event(context,s,event,x);return
    update_resize_cursor(context,s,x,y)
    if s.drag and s.drag[0]=='pack':
        pack_drag_event(context,s,event,y);return
    if s.drag and s.drag[0]=='pack_resize':
        drag=s.drag[3]
        if event.type in {'ESC','RIGHTMOUSE'} and event.value=='PRESS':
            s.pack_list_height=drag['before'];s.drag=None;s.drag_button=None
        elif event.type in {'MOUSEMOVE','INBETWEEN_MOUSEMOVE'}:
            # Dragging down shows more packs; at least one card stays visible.
            from .world_exporter_layout import PACK_CARD
            s.pack_list_height=max(PACK_CARD,drag['height']+(drag['start']-y))
        elif event.type=='LEFTMOUSE' and event.value=='RELEASE':s.drag=None;s.drag_button=None
        return
    if s.pack_search_focused and not s.menu and pack_search_event(context,s,event):return
    if event.type=='RIGHTMOUSE' and event.value=='PRESS' and s.ui_ready and not s.menu and s.inspector_open and not s.drag:
        card=pack_card_at(s,x,y)
        if card is not None:
            s.context_pack,s.context_pack_key=card[2],card[1]
            action(context,s,('menu','pack actions'))
            return
    if event.type=='ESC' and event.value=='PRESS' and s.drag:
        if s.drag_snapshot:restore_selection(settings,s.drag_snapshot)
        s.drag=None;s.drag_snapshot=None;s.drag_button=None;return
    if event.ctrl and event.type=='Z' and event.value=='PRESS' and not s.menu and not s.edit:
        action(context,s,('selection_redo',) if getattr(event,'shift',False) else ('selection_undo',));return
    if event.type in {'HOME','NUMPAD_PERIOD'} and event.value=='PRESS' and not s.menu and not s.edit and s.ui_ready:
        action(context,s,('preview_reset',) if s.preview.on else ('fit',) if s.has_selection else ('fit_world',));return
    if s.menu:
        if event.value=='PRESS' and (event.type=='LEFTMOUSE' or event.type=='BACK_SPACE' or event.unicode):
            s.worlds_keyboard_focus=False
        if event.type=='ESC' and event.value=='PRESS':s.menu=None
        elif event.type=='TAB' and event.value=='PRESS' and s.menu=='worlds':
            action(context,s,('search_focus',))
        elif event.ctrl and event.type=='R' and event.value=='PRESS' and s.menu=='worlds':
            action(context,s,('job','WORLDS'))
        elif event.ctrl and event.type=='O' and event.value=='PRESS' and s.menu=='worlds':
            action(context,s,('browse','world_path'))
        elif event.ctrl and event.type=='A' and event.value=='PRESS' and s.menu=='worlds':
            s.search_focused=True;s.search_select_all=True;s.search_blink_started=time.monotonic()
        elif event.ctrl and event.type=='V' and event.value=='PRESS' and s.menu=='worlds':
            s.search=('' if s.search_select_all else s.search)+context.window_manager.clipboard
            s.search_focused=True;s.search_select_all=False;s.search_blink_started=time.monotonic();s.menu_scroll=0;s.menu_index=0
        elif event.type in {'WHEELUPMOUSE','WHEELDOWNMOUSE'}:s.menu_scroll=max(0,s.menu_scroll+(1 if event.type=='WHEELDOWNMOUSE' else -1))
        elif event.type=='BACK_SPACE' and event.value=='PRESS':
            s.search='' if s.search_select_all else s.search[:-1];s.menu_scroll=0;s.menu_index=0
            s.search_focused=True;s.search_select_all=False;s.search_blink_started=time.monotonic()
        elif event.type=='LEFTMOUSE' and event.value=='PRESS':
            s.search_focused=False;s.search_select_all=False
            for rect,cmd in reversed(s.targets):
                if rect.contains(x,y):action(context,s,cmd);break
            else:
                if not s.menu_rect.contains(x,y):s.menu=None
        elif event.type in {'UP_ARROW','DOWN_ARROW'} and event.value=='PRESS':
            s.worlds_keyboard_focus=True
            items=[i for i in menu_items(context,s) if s.search.lower() in i[0].lower()]
            if items:
                s.menu_index=(s.menu_index+(1 if event.type=='DOWN_ARROW' else -1))%len(items)
                count=getattr(s,'menu_visible_count',4) if s.menu=='worlds' else 4
                if s.menu_index<s.menu_scroll:s.menu_scroll=s.menu_index
                elif s.menu_index>=s.menu_scroll+count:s.menu_scroll=s.menu_index-count+1
        elif event.type in {'RET','NUMPAD_ENTER'} and event.value=='PRESS':
            items=[i for i in menu_items(context,s) if s.search.lower() in i[0].lower()]
            if items:action(context,s,items[min(s.menu_index,len(items)-1)][1])
        elif event.type=='RIGHTMOUSE' and event.value=='PRESS':s.menu=None
        elif event.value=='PRESS' and event.unicode and not event.ctrl:
            s.search=('' if s.search_select_all else s.search)+event.unicode;s.menu_scroll=0;s.menu_index=0
            s.search_focused=True;s.search_select_all=False;s.search_blink_started=time.monotonic()
        return
    if s.edit:
        if event.type=='ESC' and event.value=='PRESS':s.edit=None;return
        if event.value=='PRESS':
            if event.type in {'RET','NUMPAD_ENTER'}:commit(context,s);return
            if event.ctrl and event.type=='A':s.select_all=True;return
            if event.ctrl and event.type=='V':s.edit[1]=('' if s.select_all else s.edit[1])+context.window_manager.clipboard;s.select_all=False;return
            if event.type=='BACK_SPACE':s.edit[1]='' if s.select_all else s.edit[1][:-1];s.select_all=False;return
            if event.unicode:s.edit[1]=('' if s.select_all else s.edit[1])+event.unicode;s.select_all=False;return
    rect=s.map_rect;viewrect=tuple_rect(rect)
    in_inspector=s.ui_ready and s.inspector_open and s.inspector_rect.contains(x,y)
    side='packs' if in_inspector and s.pack_list_rect.contains(x,y) else 'right'
    in_island=any(box.contains(x,y) for box in s.overlay_rects)
    if in_island and not s.drag and event.type in {'LEFTMOUSE','MIDDLEMOUSE','RIGHTMOUSE',
            'WHEELUPMOUSE','WHEELDOWNMOUSE','TRACKPADPAN','TRACKPADZOOM','MOUSEMOVE','INBETWEEN_MOUSEMOVE'}:
        if event.type=='LEFTMOUSE' and event.value=='PRESS':
            s.focus_visible=False
            for hit,cmd in reversed(s.targets):
                if hit.contains(x,y):press_control(context,s,cmd,x);break
        return
    if not s.edit and not s.drag and event.value=='PRESS':
        if event.type=='TAB':
            cmds=[cmd for _,cmd in s.targets if cmd!=('resize_inspector',)]
            if cmds:
                index=cmds.index(s.focus) if s.focus in cmds else -1
                s.focus=cmds[(index+(-1 if getattr(event,'shift',False) else 1))%len(cmds)]
                s.focus_visible=True
            return
        if event.type in {'RET','NUMPAD_ENTER','SPACE'} and s.focus:
            if any(cmd==s.focus for _,cmd in s.targets):action(context,s,s.focus)
            return
        if event.type=='N' and s.ui_ready:action(context,s,('inspector',));return
        if event.type=='F10' and getattr(event,'shift',False) and s.ui_ready:
            action(context,s,('menu','preview actions' if s.preview.on else 'selection actions' if s.has_selection else 'map actions'));return
        if event.type=='ESC':s.mode='BOX';s.focus=None;return
    if s.preview.on or (s.drag and s.drag[0] in {'orbit','pan3d','press3d'}):
        from .world_exporter_preview import handle_event as preview_event
        if preview_event(context,s,event,x,y,in_inspector):return
    if event.type=='TRACKPADPAN':
        dx=(event.mouse_x-getattr(event,'mouse_prev_x',event.mouse_x))/s.scale
        dy=(event.mouse_y-getattr(event,'mouse_prev_y',event.mouse_y))/s.scale
        if in_inspector:s.scroll[side]=max(0,min(s.scroll_max[side],s.scroll[side]+dy))
        elif rect.contains(x,y):s.view.x-=dx/s.view.scale;s.view.z+=dy/s.view.scale
        return
    if event.type=='TRACKPADZOOM' and rect.contains(x,y) and not in_inspector:
        dy=(event.mouse_y-getattr(event,'mouse_prev_y',event.mouse_y))/s.scale
        s.view.zoom(math.exp(max(-1,min(1,dy*.01))),x,y,viewrect);return
    if event.type in {'WHEELUPMOUSE','WHEELDOWNMOUSE'}:
        up=event.type=='WHEELUPMOUSE'
        now=time.monotonic()
        # A wheel already scrolling the panel keeps scrolling when a field slides under the pointer.
        scrolling=now-getattr(s,'wheel_scrolled',-1.)<.4
        if in_inspector and not scrolling and wheel_number(context,s,x,y,1 if up else -1):return
        if in_inspector:
            s.scroll[side]=max(0,min(s.scroll_max[side],s.scroll[side]+(-55 if up else 55)));s.wheel_scrolled=now
        elif rect.contains(x,y):s.view.zoom(1.25 if up else .8,x,y,viewrect)
    elif event.type in {'LEFTMOUSE','MIDDLEMOUSE','RIGHTMOUSE'} and event.value=='PRESS':
        s.focus_visible=False
        if event.type=='LEFTMOUSE':
            for hit,cmd in reversed(s.targets):
                if not hit.contains(x,y):continue
                if cmd[0]=='pack_card' and not action_locked(s,cmd):start_pack_drag(s,cmd[1],y);return
                if cmd[0]=='pack_resize':
                    s.drag=('pack_resize',(x,y),None,dict(start=y,height=s.pack_list_listed,before=s.pack_list_height))
                    s.drag_button='LEFTMOUSE';return
                press_control(context,s,cmd,x);return
        if rect.contains(x,y) and s.ui_ready and not in_inspector:
            settle(context,s)
            pan=event.type!='LEFTMOUSE'
            if not pan and action_locked(s,('selection',)):return
            s.drag_snapshot=None if pan else selection_snapshot(settings)
            s.right_dragged=False
            s.drag_button=event.type
            s.drag=('right_pan' if event.type=='RIGHTMOUSE' else 'pan' if pan else 'select',(x,y),s.view.world(x,y,viewrect),(s.view.x,s.view.z))
            if not pan and s.mode in {'BOX','CHUNK'}:
                world=s.drag[2]
                bounds=(settings.min_x,settings.min_z,settings.max_x,settings.max_z)
                # Corners keep the opposite block fixed; edges resize one axis.
                corners=[((bounds[0],bounds[1]),(bounds[2],bounds[3])),((bounds[2]+1,bounds[1]),(bounds[0],bounds[3])),
                         ((bounds[0],bounds[3]+1),(bounds[2],bounds[1])),((bounds[2]+1,bounds[3]+1),(bounds[0],bounds[1]))]
                corner=next((opposite for point,opposite in corners if s.has_selection and max(abs(a-b) for a,b in zip(s.view.screen(*point,viewrect),(x,y)))<=10),None)
                if corner is not None:s.drag=('resize',(x,y),corner,(s.view.x,s.view.z))
                elif s.has_selection:
                    mid_x=(bounds[0]+bounds[2]+1)/2;mid_z=(bounds[1]+bounds[3]+1)/2
                    edges=[((bounds[0],mid_z),'edge_x',(bounds[2],mid_z)),((bounds[2]+1,mid_z),'edge_x',(bounds[0],mid_z)),
                           ((mid_x,bounds[1]),'edge_z',(mid_x,bounds[3])),((mid_x,bounds[3]+1),'edge_z',(mid_x,bounds[1]))]
                    edge=next(((kind,opposite) for point,kind,opposite in edges
                        if (abs(s.view.screen(*point,viewrect)[0]-x)<=8 and bounds[1]<=world[1]<=bounds[3]+1 if kind=='edge_x'
                            else abs(s.view.screen(*point,viewrect)[1]-y)<=8 and bounds[0]<=world[0]<=bounds[2]+1)),None)
                    if edge:s.drag=(edge[0],(x,y),edge[1],bounds)
                    elif bounds[0]<=world[0]<=bounds[2]+1 and bounds[1]<=world[1]<=bounds[3]+1:s.drag=('move',(x,y),world,bounds)
                    else:map_select(settings,s,world,world)
                else:map_select(settings,s,world,world)
    elif event.type=='MOUSEMOVE' and s.drag:
        kind,start,world,center=s.drag
        if kind in {'pan','right_pan'}:
            s.right_dragged |= max(abs(x-start[0]),abs(y-start[1]))>=4
            if kind=='pan' or s.right_dragged:s.view.x=center[0]-(x-start[0])/s.view.scale;s.view.z=center[1]+(y-start[1])/s.view.scale
        elif kind in {'edge_x','edge_z'}:
            end=s.view.world(x,y,viewrect)
            end=tuple(v-1e-6 if v>anchor else v for v,anchor in zip(end,world))
            bounds=drag_bounds(world,end,s.snap or s.mode=='CHUNK')
            if kind=='edge_x':settings.min_x,settings.max_x=bounds[0],bounds[2]
            else:settings.min_z,settings.max_z=bounds[1],bounds[3]
        elif kind=='move':
            end=s.view.world(x,y,viewrect);step=16 if s.snap or s.mode=='CHUNK' else 1
            dx=round((end[0]-world[0])/step)*step;dz=round((end[1]-world[1])/step)*step
            settings.min_x,settings.min_z,settings.max_x,settings.max_z=center[0]+dx,center[1]+dz,center[2]+dx,center[3]+dz
        elif s.mode in {'BOX','CHUNK'}:
            end=s.view.world(x,y,viewrect)
            if kind=='resize':end=tuple(v-1e-6 if v>anchor else v for v,anchor in zip(end,world))
            map_select(settings,s,world,end)
    elif event.type in {'LEFTMOUSE','MIDDLEMOUSE','RIGHTMOUSE'} and event.value=='RELEASE':
        if s.drag_button and event.type!=s.drag_button:return
        if s.drag:
            kind,start,world,center=s.drag
            if kind=='right_pan' and not s.right_dragged and max(abs(x-start[0]),abs(y-start[1]))<4:
                selected=s.has_selection and settings.min_x<=world[0]<=settings.max_x+1 and settings.min_z<=world[1]<=settings.max_z+1
                action(context,s,('menu','selection actions' if selected else 'map actions'))
            if kind=='select' and s.mode in {'EXCLUDE','FOREGROUND'}:map_select(settings,s,world,s.view.world(x,y,viewrect))
            if s.drag_snapshot and selection_snapshot(settings)!=s.drag_snapshot:remember_selection(s,s.drag_snapshot)
        s.drag=None;s.drag_snapshot=None;s.drag_button=None


def update_resize_cursor(context,s,x,y):
    """Up-down arrows over the pack list's resize line and while dragging it."""
    window=getattr(context,'window',None)
    if window is None:return
    wanted=bool(s.drag and s.drag[0]=='pack_resize') or (not s.drag and not s.menu and s.pack_resize_rect.contains(x,y))
    if wanted!=s.resize_cursor:
        s.resize_cursor=wanted
        try:window.cursor_set('MOVE_Y' if wanted else 'DEFAULT')
        except (AttributeError,TypeError,ReferenceError):pass


def pack_card_at(s,x,y):
    """(rect, key, active path or None, top) of the pack card under the pointer."""
    return next((card for card in reversed(s.pack_cards) if card[0].contains(x,y)),None)


def start_pack_drag(s,key,y):
    """Press on a card: a click switches the pack, dragging an active one reorders the stack."""
    card=next((card for card in s.pack_cards if card[1]==key),None)
    s.flash[repr(('pack_card',key))]=time.monotonic()
    # A filtered list hides part of the stack, so it only switches packs.
    path=card[2] if card and not s.pack_search.strip() else None
    s.drag=('pack',(s.hover[0],y),key,dict(start=y,card_top=card[3] if card else y,path=path,moved=False))
    s.drag_button='LEFTMOUSE'


def pack_drag_event(context,s,event,y):
    drag=s.drag[3];key=s.drag[2]
    if action_locked(s,('pack_card',key)) or event.type=='WINDOW_DEACTIVATE' or (event.type in {'ESC','RIGHTMOUSE'} and event.value=='PRESS'):
        s.drag=None;s.drag_button=None;return
    if event.type in {'MOUSEMOVE','INBETWEEN_MOUSEMOVE'}:
        if not drag['moved'] and (drag['path'] is None or abs(y-drag['start'])<4):return
        drag['moved']=True
        from .world_exporter_layout import PACK_CARD,GUTTER
        top=drag['card_top']+(y-drag['start'])
        drag['target']=round((s.pack_list_top-top)/(PACK_CARD+GUTTER))
    elif event.type=='LEFTMOUSE' and event.value=='RELEASE':
        s.drag=None;s.drag_button=None
        if not drag['moved']:action(context,s,('pack_card',key));return
        packs=context.scene.mcbedrock_active_packs
        target=max(0,min(len(packs)-1,drag.get('target',0)))
        source=next((i for i,p in enumerate(packs) if p.path==drag['path']),-1)
        if source>=0 and target!=source:action(context,s,('stack',drag['path'],target))


def pack_search_event(context,s,event):
    """Typing into the pack search; clicks elsewhere finish it and carry on."""
    if event.type in {'LEFTMOUSE','MIDDLEMOUSE','RIGHTMOUSE'} and event.value=='PRESS':
        if not any(cmd==('pack_search',) and hit.contains(*s.hover) for hit,cmd in s.targets):s.pack_search_focused=False
        return False
    if event.value!='PRESS' or event.type in {'MOUSEMOVE','INBETWEEN_MOUSEMOVE','TIMER','WHEELUPMOUSE','WHEELDOWNMOUSE',
                                              'TRACKPADPAN','TRACKPADZOOM','LEFT_SHIFT','RIGHT_SHIFT','LEFT_CTRL','RIGHT_CTRL'}:
        return False
    before=s.pack_search
    if event.type in {'ESC','RET','NUMPAD_ENTER','TAB'}:s.pack_search_focused=False
    elif event.ctrl and event.type=='A':s.pack_search_select_all=True
    elif event.ctrl and event.type=='V':
        s.pack_search=('' if s.pack_search_select_all else s.pack_search)+context.window_manager.clipboard.strip()
        s.pack_search_select_all=False
    elif event.type=='BACK_SPACE':
        s.pack_search='' if s.pack_search_select_all else s.pack_search[:-1];s.pack_search_select_all=False
    elif event.unicode and not event.ctrl:
        s.pack_search=('' if s.pack_search_select_all else s.pack_search)+event.unicode;s.pack_search_select_all=False
    if s.pack_search!=before:s.scroll['packs']=0
    s.pack_search_blink=time.monotonic()
    return True


def native_shortcut(s,event):
    """Blender's own shortcuts stay live over the canvas: function keys (Shift ones would
    swap this editor away), workspace cycling, and the file commands when not typing."""
    if event.value!='PRESS':return False
    if event.type in F_KEYS:return not getattr(event,'shift',False)
    if not event.ctrl:return False
    if event.type in {'PAGE_UP','PAGE_DOWN'}:return True
    if s.edit or s.menu:return False
    if event.type=='Z':return not s.history and not s.pack_search_focused
    return event.type in {'S','O','N','Q','COMMA'}


def update_native_status(owner,context,s=None):
    """Use Blender's status bar, clearing our text when this workspace yields."""
    workspace=context.workspace if s is not None else None
    text=None
    if s is not None and s.ui_ready:
        text='Drag to select, middle mouse to pan, wheel to zoom, right-click for actions'
        if s.preview.on:
            from .world_exporter_preview import status_text
            text=status_text()
        elif s.mode in {'EXCLUDE','FOREGROUND'}:
            text=('Drag to exclude chunks' if s.mode=='EXCLUDE' else 'Click foreground groups')+'. Esc to finish'
        if s.drag and s.drag[0]=='number':text='Drag to adjust, hold Shift for precision, Esc or right-click to cancel'
        elif s.drag and s.drag[0]=='pack':text='Drag to change priority; the top pack wins. Esc to cancel'
        elif s.drag and s.drag[0]=='pack_resize':text='Drag to show more or fewer packs. Esc to cancel'
        elif s.menu=='worlds':text='Type to search, Up and Down to move, Enter to load, Esc to close'
        elif s.pack_search_focused:text='Type to filter packs, Enter or Esc to finish'
    previous=getattr(owner,'status_workspace',None)
    if previous is not None and previous!=workspace:
        try:previous.status_text_set(None)
        except ReferenceError:pass
    if workspace is not None and (previous!=workspace or getattr(owner,'status_text',None)!=text):
        workspace.status_text_set(text)
    owner.status_workspace=workspace;owner.status_text=text


class MCBEDROCK_OT_world_canvas(bpy.types.Operator):
    bl_idname='mcbedrock.world_canvas'
    bl_label='World map interaction'
    bl_options={'INTERNAL'}

    def invoke(self,context,event):
        self.area=context.area.as_pointer();self.window=context.window.as_pointer()
        running.add(self.area)
        self.timer=context.window_manager.event_timer_add(1/60,window=context.window)
        _timers[self.area]=(self.window,self.timer)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self,context,event):
        if not _enabled or not context.window or context.window.as_pointer()!=self.window:
            return self.finish(context)
        area=next((a for a in context.screen.areas if a.as_pointer()==self.area),None)
        if not context.workspace.get(KEY) or not area:
            # Another workspace is showing: end the session so nothing runs behind it
            # (a modal operator that never ends can also hold off auto-save).
            # _activate_panel starts a fresh one when the map is back; the state stays.
            return self.finish(context,hidden=True)
        region=next((r for r in area.regions if r.type=='WINDOW'),None)
        if not region:return {'PASS_THROUGH'}
        with context.temp_override(area=area,region=region):
            s=state(context.scene)
            use_area(s,self.area)
            try:
                if event.type=='TIMER':
                    from .world_exporter_layout import pack_caret_visible
                    tick(context,s)
                    update_native_status(self,context,s)
                    settings=context.scene.mcbedrock_world_exporter
                    scene=context.scene
                    now=time.monotonic()
                    # Pack scans and stack edits made elsewhere redraw the pinned list;
                    # the search carets redraw when they blink, not every frame.
                    stamp=(s.epoch,s.tile_revision,s.loaded,s.failed,settings.status,region.width,region.height,search_caret_visible(s,now),
                           pack_caret_visible(s,now),s.message,s.preview.on,s.preview.mesh_revision,
                           len(scene.mcbedrock_discovered_packs),len(scene.mcbedrock_active_packs),scene.get('_mcbedrock_pack_scan_revision',0))
                    revealing=s.motion.get('_motion_active') or (s.reveal_started is not None and now-s.reveal_started<.2)
                    # Progress sweeps run for whole jobs: 30 fps is plenty.
                    sweeping=s.motion.get('_motion_sweep') and now-s.swept>=1/30
                    # Only the flat map's draw takes prepared pages; the full 3D view never does.
                    if revealing or sweeping or stamp!=s.draw_stamp or (s.prepared_pages and s.preview.shown<1):
                        if sweeping:s.swept=now
                        s.draw_stamp=stamp
                        area.tag_redraw()
                    return {'PASS_THROUGH'}
                # Blender's own timers (report banners, jobs) are not input.
                if event.type.startswith('TIMER'):return {'PASS_THROUGH'}
                from .gpu_framework import runtime
                if s.native_edit or runtime._NATIVE_DIALOG_OPEN:return {'PASS_THROUGH'}
                if not s.drag and not (area.x<=event.mouse_x<=area.x+area.width and area.y<=event.mouse_y<=area.y+area.height):
                    if s.resize_cursor:update_resize_cursor(context,s,-1,-1)
                    if leave_hover(s):area.tag_redraw()
                    return {'PASS_THROUGH'}
                # Blender's own shortcuts, and key releases the canvas never uses.
                if not s.drag and native_shortcut(s,event):return {'PASS_THROUGH'}
                if event.value=='RELEASE' and not event.type.endswith('MOUSE'):return {'PASS_THROUGH'}
                moved=event.type in {'MOUSEMOVE','INBETWEEN_MOUSEMOVE'}
                handle_event(context,s,event)
                if event.type=='WINDOW_DEACTIVATE':
                    leave_hover(s);area.tag_redraw()
                    return {'PASS_THROUGH'}
                # Hover changes nothing but tooltips, so passive motion redraws only for them.
                if not moved or s.drag or hover_redraw(s):area.tag_redraw()
                # Native header buttons need passive motion to release their
                # hover/tooltip state after the pointer enters our canvas.
                # Keep gestures captured so the underlying editor cannot drag.
                if moved and not s.drag and not s.edit:
                    return {'PASS_THROUGH'}
            except Exception as exc:
                context.scene.mcbedrock_world_exporter.status='Failed: '+str(exc)
                s.failed=True;s.pending=None
        return {'RUNNING_MODAL'}

    def cancel(self,context):
        # Window closes and file loads free the session through here.
        self.finish(context)

    def finish(self,context,hidden=False):
        update_native_status(self,context)
        scene=getattr(context,'scene',None)
        s=_states.get(scene.session_uid) if scene is not None else None
        if s is not None and hasattr(scene,'mcbedrock_world_exporter'):
            cancel_number_drag(scene.mcbedrock_world_exporter,s)
            # Another area's saved layout holds a renderer; it rebuilds when that area draws again.
            if s.area_key!=self.area:s.area_layouts.pop(self.area,None)
            if hidden:
                suspend(s);s.hidden_since=time.monotonic()
                if not bpy.app.timers.is_registered(_release_hidden):
                    bpy.app.timers.register(_release_hidden,first_interval=RELEASE_AFTER)
        running.discard(self.area)
        entry=_timers.pop(self.area,None)
        manager=getattr(context,'window_manager',None)
        if entry is not None and manager is not None and any(w.as_pointer()==entry[0] for w in manager.windows):
            try:manager.event_timer_remove(entry[1])
            except (ReferenceError,RuntimeError,ValueError):pass
        self.timer=None
        return {'CANCELLED'}


class MCBEDROCK_OT_world_edit(bpy.types.Operator):
    bl_idname='mcbedrock.world_edit'
    bl_label='Edit World Exporter Value'
    field:bpy.props.StringProperty(options={'HIDDEN'})
    integer:bpy.props.IntProperty(name='Value')
    text:bpy.props.StringProperty(name='Value')

    def invoke(self,context,event):
        settings=context.scene.mcbedrock_world_exporter
        if self.field not in settings.bl_rna.properties or action_locked(state(context.scene),('edit',self.field)):
            return {'CANCELLED'}
        value=getattr(settings,self.field)
        self.is_integer=isinstance(value,int)
        if self.is_integer:self.integer=value
        else:self.text=str(value)
        self.owner=context.scene
        state(context.scene).native_edit=True
        return context.window_manager.invoke_props_dialog(self,width=340)

    def draw(self,context):
        settings=self.owner.mcbedrock_world_exporter
        self.layout.prop(self,'integer' if self.is_integer else 'text',text=settings.bl_rna.properties[self.field].name)

    def execute(self,context):
        scene=getattr(self,'owner',context.scene)
        s=state(scene);settings=scene.mcbedrock_world_exporter
        try:
            if action_locked(s,('edit',self.field)):return {'CANCELLED'}
            before=selection_snapshot(settings)
            value=self.integer if isinstance(getattr(settings,self.field),int) else self.text
            setattr(settings,self.field,value)
            if self.field in SELECTION_FIELDS:s.has_selection=True
            if selection_snapshot(settings)!=before:remember_selection(s,before)
            return {'FINISHED'}
        finally:s.native_edit=False

    def cancel(self,context):
        state(getattr(self,'owner',context.scene)).native_edit=False


class MCBEDROCK_OT_world_browse(bpy.types.Operator):
    bl_idname='mcbedrock.world_browse'
    bl_label='Choose world exporter file or folder'
    target:bpy.props.StringProperty()
    filepath:bpy.props.StringProperty(subtype='FILE_PATH')
    directory:bpy.props.StringProperty(subtype='DIR_PATH')
    filter_glob:bpy.props.StringProperty(default='*',options={'HIDDEN'})

    def invoke(self,context,event):
        from . import world_exporter as ui
        if self.target=='preset':self.directory=str(ui.storage()/'presets');self.filter_glob='*.json;*.usd'
        elif self.target=='pack':self.filter_glob='*.zip;*.mcpack;*.mcaddon;pack.mcmeta;manifest.json'
        elif self.target=='output':
            output=context.scene.mcbedrock_world_exporter.output
            self.directory=bpy.path.abspath(output) if output else (
                str(Path(bpy.data.filepath).parent/'MiEx Exports') if bpy.data.filepath else str(ui.storage()/'exports'))
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self,context):
        settings=context.scene.mcbedrock_world_exporter
        path=Path(self.filepath or self.directory)
        try:
            if self.target=='preset':
                from ..exporters.miex.presets import read_usd,apply_region
                data=json.loads(path.read_text(encoding='utf-8')) if path.suffix.lower()=='.json' else read_usd(path)
                if 'region' in data:
                    if 'world' in data:data['world_path']=data['world']
                    for key,prop in {'removeCaves':'remove_caves','blockAnimations':'block_animations'}.items():
                        if key in data:data[prop]=data[key]
                    for key,prop in {'chunkSize':'chunk_size','runOptimiser':'run_optimiser','fillInCaves':'fill_caves',
                        'entityStartFrame':'entity_start','entityEndFrame':'entity_end','entityFPS':'entity_fps',
                        'entityRandomSeed':'entity_seed','entitySpawnDensity':'entity_density','entitySunLightLevel':'entity_light'}.items():
                        if key in data.get('options',{}):data[prop]=data['options'][key]
                    for key,prop in [('entitySimulate','entity_simulate'),('entitySpawnRules','entity_spawn')]:
                        if key in data.get('options',{}):data[prop]=','.join(data['options'][key])
                    if isinstance(data.get('entity_types'),list):data['entity_types']=','.join(data['entity_types'])
                    apply_region(settings,data['region'])
                    settings.regions=json.dumps(data.get('extra_regions',[]))
                for prop in PRESET_FIELDS:
                    if prop in data:
                        value=bool(data[prop]) if isinstance(getattr(settings,prop),bool) else data[prop]
                        setattr(settings,prop,value)
                missing=[]
                from .world_exporter_packs import restore_stack
                pack_rows=list(data.get('packs',[]))
                for pack_id in data.get('source_pack_ids',[]):
                    if pack_id in {'base_resource_pack','BlenderCycles','BlenderEEVEE'}:continue
                    match=next((p for p in context.scene.mcbedrock_discovered_packs if p.name==pack_id or Path(p.path).name==pack_id),None)
                    if match:pack_rows.append(dict(name=match.name,path=match.path))
                    else:missing.append(pack_id)
                if not missing and ('packs' in data or 'source_pack_ids' in data):
                    missing=restore_stack(context,pack_rows)
                state(context.scene).notice=('Pack stack unchanged; missing preset packs: '+', '.join(missing)) if missing else ''
                state(context.scene).pending='LOAD'
                state(context.scene).has_selection=True
                state(context.scene).preset_selection=True
            elif self.target=='pack':
                if path.name in {'pack.mcmeta','manifest.json'}:path=path.parent
                from .world_exporter_packs import add_source
                add_source(context,str(path))
            else:
                if path.is_file():path=path.parent
                setattr(settings,self.target,str(path))
                if self.target=='world_path':state(context.scene).pending='LOAD'
            return {'FINISHED'}
        except (OSError,ValueError,TypeError,KeyError) as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}


def register():
    global _handle,_enabled
    _enabled=True
    for cls in (MCBEDROCK_OT_world_canvas,MCBEDROCK_OT_world_browse,MCBEDROCK_OT_world_edit):bpy.utils.register_class(cls)
    _handle=bpy.types.SpaceImageEditor.draw_handler_add(draw_canvas,(),'WINDOW','POST_PIXEL')


def unregister():
    global _handle,_enabled
    _enabled=False
    for workspace in bpy.data.workspaces:
        if workspace.get(KEY):workspace.status_text_set(None)
    if _handle is not None:
        bpy.types.SpaceImageEditor.draw_handler_remove(_handle,'WINDOW');_handle=None
    if bpy.app.timers.is_registered(_release_hidden):bpy.app.timers.unregister(_release_hidden)
    reset()
    world_map_renderer.reset()
    from . import world_preview_renderer
    world_preview_renderer.reset()
    for cls in (MCBEDROCK_OT_world_edit,MCBEDROCK_OT_world_browse,MCBEDROCK_OT_world_canvas):
        if hasattr(bpy.types,cls.__name__):bpy.utils.unregister_class(cls)
