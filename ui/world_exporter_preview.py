"""World Exporter 3D preview: the selected area in 3D, orbitable like Mine-imator's world import.

The Flat | 3D switch at the map's top-left swaps the map for MiEx preview
meshes of the selected area, cut sides included. Worlds open in Flat. The job thread fetches and
decodes them in small batches, nearest the centre first
(exporters/miex/preview.py). This module keeps them per chunk, follows area
edits, and turns pointer input into an orbit camera. GPU drawing lives in
world_preview_renderer. When the first chunks arrive, the top-down map tilts
into 3D.
"""
import json
import math
import time
from .gpu_framework import Rect
from .gpu_framework.motion import tween
from ..exporters.miex.preview import (area_chunks, chunk_rows, plan, row_key, surface_floor,
                                      BATCH_FACES, FACES, MAX_BATCH, MAX_CHUNKS)

FOV = 40.0
# Resting view, in degrees: from the south-east, looking down a third of the way.
YAW, PITCH = 35.0, 32.0
# The map's own view, where the tilt into 3D starts.
TOP_DOWN = 89.0
TILT = .7
PITCH_RANGE = (2.0, 89.0)
# Degrees of orbit per logical pixel dragged.
ORBIT = .35
# An edited area waits this long before reloading, so scrubbing a field stays smooth.
SETTLE = .35
# Share of the view the area's box may fill.
FIT = .86
# The Flat | 3D switch: a 30 px segmented track, 16 px in from the map's corner.
VIEWS = (('flat','Flat'),('3d','3D'))
SWITCH = 30
SEGMENT = 56
INSET = 16


class Preview:
    def __init__(self):
        self.on = False
        self.epoch = 0
        self.area = None
        self.pending_area = None
        self.pending_since = 0.0
        # An area waiting for its map tiles: they give each chunk's surface height.
        self.target = None
        self.target_chunks = []
        self.tiles_asked = set()
        self.step = 1           # the coarsest block step in use
        self.plan = None        # (core step, core chunks, outer step)
        self.rows = {}          # chunk -> its wanted preview rows, centre first
        self.steps = {}         # chunk -> its block step
        self.keys = {}          # chunk -> row key of its wanted rows
        self.meshes = {}        # chunk -> decoded arrays, or None for an empty chunk
        self.loaded_keys = {}   # chunk -> row key the stored mesh answers; older ones show until replaced
        self.requested = {}     # chunk -> row key in flight
        self.loaded = 0
        self.terrain = False
        self.bounds = None
        self.too_large = False
        self.failed = False
        self.revision = 0
        self.mesh_revision = 0
        self.camera = camera()
        self.goal = camera()
        self.user_camera = False
        self.tilt_started = None
        self.tilt_from = None
        self.last_advance = None
        self.shown = 0.0
        self.aspect = 16/9
        self.scene = None

    # Lifecycle -----------------------------------------------------------
    def turn_on(self):
        self.on = True
        self.failed = False
        self.user_camera = False
        self.tilt_started = None

    def turn_off(self):
        self.on = False
        self.tilt_started = None

    def invalidate(self):
        """The world, dimension or packs changed: every mesh is stale."""
        self.epoch += 1
        self.meshes.clear();self.loaded_keys.clear();self.requested.clear()
        self.target = None;self.target_chunks = [];self.tiles_asked.clear()
        self.loaded = 0;self.terrain = False;self.bounds = None
        self.failed = False
        self.area = None
        self.revision += 1
        self.mesh_revision += 1
        if self.scene is not None:self.scene.free()

    def free_gpu(self):
        if self.scene is not None:self.scene.free()
        self.scene = None

    # Area and requests ---------------------------------------------------
    def sync(self, s, settings, now):
        """Follow the selection; called from the canvas timer."""
        if not self.on:return
        if not (s.loaded and s.ui_ready and s.has_selection):
            self.turn_off();return
        area = area_of(settings)
        if area == (self.target or self.area):
            self.pending_area = None
        else:
            if area != self.pending_area:
                self.pending_area, self.pending_since = area, now
            if self.area is None or now-self.pending_since >= SETTLE:
                self.target, self.pending_area = area, None
                self.target_chunks = chunks_of(area)
        if self.target is not None and not self.missing_tiles(s):
            target, self.target = self.target, None
            self.apply(target,s.tiles)

    def missing_tiles(self, s):
        """Area chunks the map has not drawn yet; none when the map failed or the area is too large."""
        if self.target is None or s.failed or len(self.target_chunks) > MAX_CHUNKS:return []
        return [key for key in self.target_chunks if key not in s.tiles]

    def next_tiles(self, s, limit=256):
        """Map tiles to ask for before planning, so every chunk knows its ground."""
        missing = [key for key in self.missing_tiles(s) if key not in self.tiles_asked][:limit]
        self.tiles_asked.update(missing)
        return missing

    def apply(self, area, tiles=None):
        """Plan the area's rows. Map tiles give each chunk's lowest surface column."""
        previous = self.area
        self.area, self.pending_area = area, None
        self.tiles_asked.clear()
        x0,x1,y0,y1,z0,z1,_ = area
        chunks = chunks_of(area)
        self.plan = plan(len(chunks))
        self.too_large = self.plan is None
        self.step = self.plan[2] if self.plan else None
        surface = {key:surface_floor(tiles.get(key)) for key in chunks} if tiles else {}
        entries = chunk_rows((x0,x1,y0,y1,z0,z1),self.plan,chunks,surface) if self.plan else []
        self.rows = {key:rows for key,_,rows in entries}
        self.steps = {key:step for key,step,_ in entries}
        self.keys = {key:row_key(rows) for key,rows in self.rows.items()}
        # Chunks outside the area go now; edited ones keep showing until their new mesh arrives.
        for key in list(self.meshes):
            if key not in self.keys:
                self.meshes.pop(key,None);self.loaded_keys.pop(key,None)
        self.failed = False
        self.recount()
        moved = previous is None or previous[:2] != area[:2] or previous[4:6] != area[4:6]
        if moved or not self.user_camera:
            self.frame()
            if previous is None:self.camera = dict(self.goal)

    def next_rows(self):
        """The next batch of rows to fetch, about BATCH_FACES faces, or [] when nothing is missing."""
        if not self.on or self.failed or self.too_large or self.area is None:return []
        rows = []
        faces = chunks = 0
        for key,wanted in self.rows.items():
            if self.loaded_keys.get(key) == self.keys[key] or self.requested.get(key) == self.keys[key]:continue
            rows += wanted
            self.requested[key] = self.keys[key]
            faces += FACES[self.steps[key]];chunks += 1
            if faces >= BATCH_FACES or chunks >= MAX_BATCH:break
        return rows

    def receive(self, job, decoded):
        # A chunk's rows travel together and in order, so they rebuild its key.
        groups = {}
        for row in job.get('chunks',()):
            groups.setdefault((row['chunkX'],row['chunkZ']),[]).append(row)
        for key,rows in groups.items():
            wanted = row_key(rows)
            if self.requested.get(key) == wanted:self.requested.pop(key,None)
            if job.get('preview_epoch') != self.epoch or self.keys.get(key) != wanted:continue
            self.meshes[key] = decoded.get(key)
            self.loaded_keys[key] = wanted
        had_bounds = self.bounds is not None
        self.recount()
        # Frame the first terrain, and again once everything is in, unless the user moved the view.
        if not self.user_camera and (not had_bounds or self.complete()):self.frame()

    def fail(self, job):
        if job.get('preview_epoch') != self.epoch:return
        self.failed = True
        self.requested.clear()
        self.revision += 1

    def reload(self):
        self.invalidate()

    def recount(self):
        self.loaded = sum(1 for key,wanted in self.keys.items() if self.loaded_keys.get(key) == wanted)
        boxes = [self.meshes[key][3] for key in self.rows if self.meshes.get(key) is not None]
        self.terrain = bool(boxes)
        self.bounds = ((min(b[0][0] for b in boxes),min(b[0][1] for b in boxes),min(b[0][2] for b in boxes)),
                       (max(b[1][0] for b in boxes),max(b[1][1] for b in boxes),max(b[1][2] for b in boxes))) if boxes else None
        self.revision += 1
        self.mesh_revision += 1

    def complete(self):
        return self.target is None and self.area is not None and self.loaded >= len(self.rows)

    def ready(self):
        """Something to show in 3D: terrain, or a reason there is none."""
        return self.too_large or self.failed or self.terrain or (self.complete() and self.area is not None)

    # Camera --------------------------------------------------------------
    def y_range(self):
        x0,x1,y0,y1,z0,z1,_ = self.area
        if self.bounds:
            low,high = max(y0,self.bounds[0][1]),min(y1+1,self.bounds[1][1])
            if high > low:return low,high
        return y0,min(y1+1,max(y0+1,96))

    def frame(self):
        """Aim the goal camera at the area: its x/z extent and the terrain's height,
        close enough that the whole box fills the view from the goal's angle."""
        if self.area is None:return
        x0,x1,_,_,z0,z1,_ = self.area
        low,high = self.y_range()
        sx,sy,sz = x1-x0+1,max(1,high-low),z1-z0+1
        target = ((x0+x1+1)/2,(low+high)/2,(z0+z1+1)/2)
        corners = [(x,y,z) for x in (x0,x1+1) for y in (low,high) for z in (z0,z1+1)]
        self.goal.update(target=target,radius=.5*math.sqrt(sx*sx+sy*sy+sz*sz),
                         distance=max(6.,fit_distance(self.goal['yaw'],self.goal['pitch'],target,corners,self.aspect)))
        self.revision += 1

    def reset_view(self):
        self.user_camera = False
        self.goal.update(yaw=YAW,pitch=PITCH)
        self.frame()

    def start_tilt(self, now):
        """Begin at the map's top-down view, north up, and tilt into the resting angle."""
        self.camera = dict(self.goal,yaw=0.,pitch=TOP_DOWN)
        self.tilt_from = (0.,TOP_DOWN)
        self.tilt_started = now
        self.last_advance = now

    def advance(self, now, still):
        """Ease the camera toward its goal; True while it is still moving."""
        dt = 0. if self.last_advance is None else max(0.,min(.1,now-self.last_advance))
        self.last_advance = now
        if still:
            self.camera = dict(self.goal);self.tilt_started = None
            return False
        moving = False
        angles = None
        if self.tilt_started is not None:
            t = min(1.,(now-self.tilt_started)/TILT)
            e = 4*t**3 if t < .5 else 1-(-2*t+2)**3/2
            start_yaw,start_pitch = self.tilt_from
            yaw_gap = ((self.goal['yaw']-start_yaw+180)%360)-180
            angles = (start_yaw+yaw_gap*e,start_pitch+(self.goal['pitch']-start_pitch)*e)
            if t >= 1:self.tilt_started = None
            else:moving = True
        k = 1-math.exp(-dt*10)
        camera = self.camera
        if angles is not None:camera['yaw'],camera['pitch'] = angles
        else:
            gap = ((self.goal['yaw']-camera['yaw']+180)%360)-180
            camera['yaw'] = self.goal['yaw'] if abs(gap) < .01 else camera['yaw']+gap*k
            camera['pitch'] = _approach(camera['pitch'],self.goal['pitch'],k,.01)
            moving |= abs(gap) >= .01 or camera['pitch'] != self.goal['pitch']
        for name in ('distance','radius'):
            camera[name] = _approach(camera[name],self.goal[name],k,camera[name]*1e-4)
            moving |= camera[name] != self.goal[name]
        target = tuple(_approach(a,b,k,.001) for a,b in zip(camera['target'],self.goal['target']))
        moving |= target != tuple(self.goal['target'])
        camera['target'] = target
        if moving:self.revision += 1
        return moving

    def orbit(self, dx, dy):
        self.user_camera = True
        if self.tilt_started is not None:
            # Grabbing the view mid-tilt keeps it where it is.
            self.goal['yaw'],self.goal['pitch'] = self.camera['yaw'],self.camera['pitch']
            self.tilt_started = None
        for cam in (self.camera,self.goal):
            cam['yaw'] -= dx*ORBIT
            cam['pitch'] = max(PITCH_RANGE[0],min(PITCH_RANGE[1],cam['pitch']-dy*ORBIT))
        self.revision += 1

    def pan(self, dx, dy, height):
        self.user_camera = True
        cam = self.camera
        scale = 2*cam['distance']*math.tan(math.radians(FOV)/2)/max(1.,height)
        yaw,pitch = math.radians(cam['yaw']),math.radians(cam['pitch'])
        forward = (-math.cos(pitch)*math.sin(yaw),-math.sin(pitch),-math.cos(pitch)*math.cos(yaw))
        right = _normal((-forward[2],0.,forward[0]))
        up = (right[1]*forward[2]-right[2]*forward[1],right[2]*forward[0]-right[0]*forward[2],right[0]*forward[1]-right[1]*forward[0])
        shift = tuple(-(r*dx+u*dy)*scale for r,u in zip(right,up))
        for c in (self.camera,self.goal):
            c['target'] = tuple(t+d for t,d in zip(c['target'],shift))
        self.revision += 1

    def zoom(self, factor):
        self.user_camera = True
        radius = max(1.,self.goal['radius'])
        for cam in (self.camera,self.goal):
            cam['distance'] = max(max(3.,radius*.12),min(radius*8,cam['distance']*factor))
        self.revision += 1

    def selection_lines(self, color, width=1.5):
        """The area's box, from the terrain's lowest to highest face, as line segments."""
        if self.area is None or not self.terrain:return None
        x0,x1,_,_,z0,z1,_ = self.area
        low,high = self.y_range()
        # A hair outside the terrain, so the edges never sink into the faces they frame.
        m = .08*(self.step or 1)
        xs,zs,ys = (x0-m,x1+1+m),(z0-m,z1+1+m),(low-m,high+m)
        corners = [(x,y,z) for x in xs for y in ys for z in zs]
        segments = []
        for i,a in enumerate(corners):
            for b in corners[i+1:]:
                if sum(p != q for p,q in zip(a,b)) == 1:segments += [a,b]
        return tuple(segments),tuple(color),width

    def gpu_scene(self):
        if self.scene is None:
            from .world_preview_renderer import Scene
            self.scene = Scene()
        return self.scene


def camera():
    return dict(yaw=YAW,pitch=PITCH,distance=96.,target=(0.,64.,0.),radius=64.,fov=FOV)


def fit_distance(yaw, pitch, target, corners, aspect, fill=FIT):
    """The orbit distance at which every corner projects inside `fill` of the view."""
    yaw,pitch = math.radians(yaw),math.radians(pitch)
    back = (math.cos(pitch)*math.sin(yaw),math.sin(pitch),math.cos(pitch)*math.cos(yaw))
    right = _normal((back[2],0.,-back[0]))
    up = (back[1]*right[2]-back[2]*right[1],back[2]*right[0]-back[0]*right[2],back[0]*right[1]-back[1]*right[0])
    focal = 1/math.tan(math.radians(FOV)/2)
    distance = 0.
    for corner in corners:
        d = tuple(c-t for c,t in zip(corner,target))
        x = sum(a*b for a,b in zip(d,right))
        y = sum(a*b for a,b in zip(d,up))
        depth = max(abs(x)*focal/(max(aspect,1e-3)*fill),abs(y)*focal/fill)
        distance = max(distance,sum(a*b for a,b in zip(d,back))+depth)
    return distance


def _approach(value, goal, k, epsilon):
    value += (goal-value)*k
    return goal if abs(goal-value) <= epsilon else value


def _normal(v):
    length = math.sqrt(sum(c*c for c in v)) or 1.
    return tuple(c/length for c in v)


def chunks_of(area):
    x0,x1,_,_,z0,z1,excluded = area
    try:skipped = json.loads(excluded)
    except (TypeError, ValueError):skipped = []
    return area_chunks(x0,x1,z0,z1,skipped)


def area_of(settings):
    x0,x1 = sorted((settings.min_x,settings.max_x))
    y0,y1 = sorted((settings.min_y,settings.max_y))
    z0,z1 = sorted((settings.min_z,settings.max_z))
    return x0,x1,y0,y1,z0,z1,settings.excluded


# Canvas hooks ----------------------------------------------------------------

def toggle(s, now=None):
    set_view(s,'flat' if s.preview.on else '3d')


def set_view(s, view):
    """Flat shows the map; 3D needs an area."""
    preview = s.preview
    if view == 'flat':
        if preview.on:preview.turn_off()
    elif not preview.on and s.has_selection and s.loaded:
        preview.turn_on()
        s.mode = 'BOX'


def status_text():
    return 'Drag to orbit, middle mouse or Shift-drag to pan, wheel to zoom, right-click for actions'


def handle_event(context, s, event, x, y, in_inspector):
    """Pointer input over the 3D view. Returns True when the event was used."""
    preview = s.preview
    rect = s.map_rect
    if s.drag and s.drag[0] in {'orbit','pan3d','press3d'}:
        kind,start,last,_ = s.drag
        if event.type in {'MOUSEMOVE','INBETWEEN_MOUSEMOVE'}:
            dx,dy = x-last[0],y-last[1]
            if kind == 'press3d' and max(abs(x-start[0]),abs(y-start[1])) >= 4:
                kind = 'pan3d';dx,dy = x-start[0],y-start[1]
            if kind == 'orbit':preview.orbit(dx,dy)
            elif kind == 'pan3d':preview.pan(dx,dy,rect.height)
            s.drag = (kind,start,(x,y),None)
            return True
        if event.value == 'RELEASE' and event.type == s.drag_button:
            s.drag = None;s.drag_button = None
            if kind == 'press3d':
                from .world_exporter_canvas import action
                action(context,s,('menu','preview actions'))
            return True
        return event.type not in {'ESC','WINDOW_DEACTIVATE'}
    if in_inspector or not rect.contains(x,y) or s.menu:return False
    if event.type in {'WHEELUPMOUSE','WHEELDOWNMOUSE'}:
        preview.zoom(.85 if event.type == 'WHEELUPMOUSE' else 1/.85);return True
    if event.type == 'TRACKPADPAN':
        dx = (event.mouse_x-getattr(event,'mouse_prev_x',event.mouse_x))/s.scale
        dy = (event.mouse_y-getattr(event,'mouse_prev_y',event.mouse_y))/s.scale
        if getattr(event,'shift',False):preview.pan(dx,dy,rect.height)
        else:preview.orbit(-dx,-dy)
        return True
    if event.type == 'TRACKPADZOOM':
        dy = (event.mouse_y-getattr(event,'mouse_prev_y',event.mouse_y))/s.scale
        preview.zoom(math.exp(-max(-1,min(1,dy*.01))));return True
    if event.type in {'LEFTMOUSE','MIDDLEMOUSE','RIGHTMOUSE'} and event.value == 'PRESS':
        if any(hit.contains(x,y) for hit,_ in s.targets):return False
        s.focus_visible = False
        kind = ('pan3d' if event.type == 'MIDDLEMOUSE' or (event.type == 'LEFTMOUSE' and getattr(event,'shift',False))
                else 'press3d' if event.type == 'RIGHTMOUSE' else 'orbit')
        s.drag = (kind,(x,y),(x,y),None);s.drag_button = event.type;s.drag_snapshot = None
        return True
    return False


def menu_items(s):
    return [('Reset view (Home)',('preview_reset',)),('Reload 3D preview',('preview_reload',)),
            ('Back to the flat map',('view','flat')),
            ('Hide panel (N)' if s.inspector_open else 'Show panel (N)',('inspector',)),
            ('Back to Layout',('layout',),{'divider':True})]


def switch_rect(s):
    rect = s.map_rect
    return Rect(rect.x+INSET,rect.top-INSET-SWITCH,SEGMENT*len(VIEWS),SWITCH)


def draw_switch(paint, s):
    """The Flat | 3D switch floating on the map, like the dimension tabs.

    An opaque #141414 track with the hairline edge and 4 px control corners; the
    open view sits on a Sparkle Blue pill that slides over 0.22 s. 3D waits for an
    area: its label mutes and it takes no clicks. Returns the track rect, which
    the canvas treats as an island so a click never starts a map drag."""
    from .world_exporter_layout import T, EDGE
    from .world_exporter_canvas import action_locked, CONTROL_RADIUS
    r = paint.r
    track = switch_rect(s)
    r.rect(track,T.canvas,radius=CONTROL_RADIUS,border=EDGE)
    selected = 1 if s.preview.on else 0
    position = paint.ease('view.switch',selected,.22)
    r.rect(Rect(track.x+position*SEGMENT+2,track.y+2,SEGMENT-4,track.height-4),T.accent,radius=CONTROL_RADIUS-1)
    for i,(view,label) in enumerate(VIEWS):
        cell = Rect(track.x+i*SEGMENT,track.y,SEGMENT,track.height)
        command = ('view',view)
        active = i == selected
        enabled = (view == 'flat' or s.has_selection or s.preview.on) and not action_locked(s,command)
        ink = (1,1,1,1) if active else T.text_muted if enabled else T.text_muted[:3]+(.6,)
        r.text(cell,label,ink,size=11.5,weight='SemiBold' if active else 'Medium',align='CENTER')
        if paint.focused(command):r.rect(cell.inset((2,2,2,2)),(0,0,0,0),radius=CONTROL_RADIUS,border=T.text,border_width=1.5)
        if enabled:paint.target(cell,command)
    return track


def draw(context, s, paint, settings, rect, flat_map):
    """The map rect while the preview is on or fading: the map below, the 3D view over it."""
    from .world_exporter_layout import T, MAP_GROUND, fit_text
    r = paint.r
    preview = s.preview
    motion = s.motion
    now = motion.get('_motion_time',time.monotonic())
    still = bool(motion.get('_motion_disabled'))
    ready = preview.on and preview.ready()
    shown = tween(motion,'miex.preview.shown',1. if ready else 0.,.2,initial=0.)
    preview.shown = shown
    if shown < 1:
        flat_map()
        if preview.on and not ready:
            # Waiting for the first chunks: the map dims and a card says why.
            r.push_clip(rect);r.rect(rect,(0,0,0,.35));r.pop_clip()
    if ready and preview.terrain and preview.tilt_started is None and preview.last_advance is None:
        preview.start_tilt(now)
    if shown > 0:
        preview.aspect = rect.width/max(1.,rect.height)
        if preview.advance(now,still):motion['_motion_active'] = True
        scene = preview.gpu_scene()
        scene.sync(preview.meshes,preview.rows.keys(),preview.mesh_revision)
        box = r._physical_rect(rect)
        clear = r._color(MAP_GROUND)
        lines = preview.selection_lines(r._color(T.accent[:3]+(.55,)),1.5*r.ui_scale)
        scene.render(box.width,box.height,preview.camera,lines,clear,preview.revision)
        r.push_clip(rect)
        try:
            scene.draw(r,rect,shown*r.opacity)
            opacity = r.opacity
            r.opacity = opacity*shown
            try:_draw_notes(paint,preview,rect,T,fit_text)
            finally:r.opacity = opacity
        finally:r.pop_clip()
    elif not preview.on:
        # Fully back on the map: release the GPU copy, keep the decoded chunks.
        preview.free_gpu()
        preview.last_advance = None
    if preview.on and not preview.complete() and not preview.too_large and not preview.failed:
        _draw_loading(paint,s,preview,rect,T,fit_text)


def _draw_notes(paint, preview, rect, T, fit_text):
    """Why the 3D view is empty, centred on it."""
    r = paint.r
    message = ''
    if preview.too_large:
        side = int(math.sqrt(MAX_CHUNKS))*16
        message = f'This area is too large to show in 3D. Select up to {side:,} × {side:,} blocks.'
    elif preview.failed:message = 'The 3D preview could not load. Right-click and choose Reload 3D preview.'
    elif preview.complete() and not preview.terrain:message = 'Nothing to show in this area. Check Min Y and Max Y.'
    if message:
        # Wrapped, so a narrow map keeps the instruction at the end of the message.
        from .world_exporter_layout import wrap
        lines = wrap(message,rect.width-40,12,lines=3)
        top = rect.y+rect.height/2+9*len(lines)
        for i,line in enumerate(lines):
            r.text(Rect(rect.x+20,top-18*(i+1),rect.width-40,18),fit_text(r,line,rect.width-40,12),T.text_muted,size=12,align='CENTER')


def _draw_loading(paint, s, preview, rect, T, fit_text):
    """A small card at the bottom of the map with chunk progress."""
    from .world_exporter_view import card_frame
    r = paint.r
    width = min(300,max(0,rect.width-32))
    if width < 120:return
    card = Rect(rect.x+(rect.width-width)/2,rect.y+16,width,56)
    card_frame(r,card)
    total = max(1,len(preview.rows))
    count = f'{preview.loaded:,} of {len(preview.rows):,} chunks' if preview.rows and preview.target is None else ''
    # On a narrow map the title gives way to the count instead of running into it.
    from .world_exporter_layout import text_width
    room = width-32-(text_width(r,count,11)+8 if count else 0)
    r.text(Rect(card.x+16,card.top-30,room,18),fit_text(r,'Building 3D preview',room,12.5,'SemiBold'),T.text,size=12.5,weight='SemiBold')
    if count:
        r.text(Rect(card.x+16,card.top-30,width-32,18),count,T.text_muted,size=11,align='RIGHT')
    track = Rect(card.x+16,card.y+14,width-32,2)
    r.rect(track,T.surface_raised,radius=1)
    if preview.loaded and preview.target is None:
        done = Rect(track.x,track.y,track.width*min(1.,preview.loaded/total),track.height)
        r.rect(done,T.accent,radius=1)
    else:
        from .world_exporter_layout import sweep
        sweep(r,s,track)
