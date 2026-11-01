"""World Exporter 3D preview: the selected area in 3D, orbitable like Mine-imator's world import.

The Flat | 3D switch at the map's top-left swaps the map for MiEx preview
meshes of the selected area, cut sides included. Worlds open in Flat. The job thread fetches and
decodes them in small batches, nearest the centre first
(exporters/miex/preview.py). This module keeps them per chunk, follows area
edits, and turns pointer input into an orbit camera. Two handles on the area's
box drag Max Y (top) and Min Y (bottom) in whole blocks. GPU drawing lives in
world_preview_renderer. When the first chunks arrive, the top-down map tilts
into 3D.
"""
import json
import math
import time
from .gpu_framework import Rect
from .gpu_framework.motion import tween, blend
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
# The Max Y and Min Y handles: 12 px dots (14 px under the pointer) taken within
# HANDLE_HIT px, kept HANDLE_GAP px apart when the box is seen from above.
HANDLE = 12
HANDLE_HIT = 12
HANDLE_GAP = 28
HANDLES = {'max_y':'Max Y','min_y':'Min Y'}
# A handle drag moves at most two blocks per pixel, however short the vertical axis looks.
MIN_RATE = .5
# Minecraft's build height since 1.18, Java and Bedrock; typed values beyond it widen it.
WORLD_HEIGHT = (-64, 319)
# Heights the box eases to over this long; Reduce motion snaps.
BOX_EASE = .2
# Times a chunk MiEx could not mesh is asked for before it is left out (and said why).
CHUNK_TRIES = 3


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
        self.tries = {}         # chunk -> failed attempts at its wanted rows
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
        self.meshes.clear();self.loaded_keys.clear();self.requested.clear();self.tries.clear()
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
        self.tries.clear()
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
        failed = {}
        for row in job.get('preview_failed') or ():
            try:failed[int(row['chunkX']),int(row['chunkZ'])] = row.get('reason','')
            except (KeyError,TypeError,ValueError):continue
        for key,rows in groups.items():
            wanted = row_key(rows)
            if self.requested.get(key) == wanted:self.requested.pop(key,None)
            if job.get('preview_epoch') != self.epoch or self.keys.get(key) != wanted:continue
            # A chunk MiEx could not mesh this time stays wanted, so next_rows asks again;
            # stored as empty it never came back. After CHUNK_TRIES it shows what arrived.
            if key in failed:
                self.tries[key] = self.tries.get(key,0)+1
                if self.tries[key] < CHUNK_TRIES:continue
                print(f'3D preview left out chunk {key[0]}, {key[1]} after {self.tries[key]} tries: {failed[key] or "no reason given"}')
            self.tries.pop(key,None)
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
    def y_range(self, live=None):
        """The box's height: Min Y to Max Y + 1, trimmed to the terrain where the loaded
        meshes show it ends inside the area, so the box hugs the ground.

        `live` holds the fields' current (Min Y, Max Y), which the box follows at once while
        the meshes wait for SETTLE. Terrain that reaches the area's cut may go on past it
        unloaded, so there the box shows the field's value instead of the old meshes' edge.
        """
        _,_,cut_low,cut_high,_,_,_ = self.area
        y0,y1 = live if live is not None else (cut_low,cut_high)
        low,high = y0,y1+1
        if self.bounds:
            ground,top = self.bounds[0][1],self.bounds[1][1]
            if top <= cut_high+1e-3:high = min(high,top)
            if ground >= cut_low+1-1e-3:low = max(low,ground)
            if high > low:return low,high
        return y0,min(y1+1,max(y0+1,96))

    def box_span(self, live, held=None):
        """(bottom, top) of the box for the fields' values; the face being dragged sits on its value."""
        low,high = self.y_range(live)
        if held == 'max_y':high = live[1]+1
        elif held == 'min_y':low = live[0]
        return low,high

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

    def hold(self):
        """A handle drag keeps the camera still: the tilt stops, and nothing re-frames afterwards."""
        self.user_camera = True
        self.tilt_started = None
        self.goal = dict(self.camera)
        self.revision += 1

    def margin(self):
        """A hair outside the terrain, so the box's edges never sink into the faces they frame."""
        return .08*(self.step or 1)

    def facing_side(self, camera, span):
        """(x, z) at the middle of the box's vertical side that faces the camera most."""
        x0,x1,_,_,z0,z1,_ = self.area
        m = self.margin()
        west,east,north,south = x0-m,x1+1+m,z0-m,z1+1+m
        mid_x,mid_z,mid_y = (west+east)/2,(north+south)/2,sum(span)/2
        eye = view_basis(camera)[0]
        sides = (((west,mid_z),(-1,0)),((east,mid_z),(1,0)),((mid_x,north),(0,-1)),((mid_x,south),(0,1)))

        def facing(side):
            (x,z),(nx,nz) = side
            d = (eye[0]-x,eye[1]-mid_y,eye[2]-z)
            return (d[0]*nx+d[2]*nz)/(math.sqrt(d[0]*d[0]+d[1]*d[1]+d[2]*d[2]) or 1.)
        return max(sides,key=facing)[0]

    def handles(self, camera, rect, span, anchor=None):
        """[(prop, (x, y), anchor)]: the Max Y and Min Y handles in the map rect.

        They sit in the middle of the top and bottom edges of the side facing the camera
        (`anchor`, its x and z, stays put while one is dragged). The terrain never hides
        that side's edges, and seen from above the two spread HANDLE_GAP apart along the
        vertical axis, so both stay visible and can be grabbed from any orbit angle.
        """
        low,high = span
        m = self.margin()
        if anchor is None:anchor = self.facing_side(camera,span)
        x,z = anchor
        top,bottom = project(camera,rect,(x,high+m,z)),project(camera,rect,(x,low-m,z))
        if top is not None and bottom is not None:
            gap = math.hypot(top[0]-bottom[0],top[1]-bottom[1])
            if gap < HANDLE_GAP:
                ux,uy = ((top[0]-bottom[0])/gap,(top[1]-bottom[1])/gap) if gap > .5 else (0.,1.)
                mx,my = (top[0]+bottom[0])/2,(top[1]+bottom[1])/2
                top = (mx+ux*HANDLE_GAP/2,my+uy*HANDLE_GAP/2)
                bottom = (mx-ux*HANDLE_GAP/2,my-uy*HANDLE_GAP/2)
        return [(prop,point,anchor) for prop,point in (('max_y',top),('min_y',bottom)) if point is not None]

    def selection_lines(self, color, width=1.5, span=None):
        """The area's box as line segments, `span` (or y_range) tall."""
        if self.area is None or not self.terrain:return None
        x0,x1,_,_,z0,z1,_ = self.area
        low,high = span or self.y_range()
        m = self.margin()
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


def view_basis(camera):
    """(eye, right, up, forward) of an orbit camera, as world_preview_renderer.camera_matrices builds it."""
    yaw,pitch = math.radians(camera['yaw']),math.radians(camera['pitch'])
    back = (math.cos(pitch)*math.sin(yaw),math.sin(pitch),math.cos(pitch)*math.cos(yaw))
    eye = tuple(t+b*camera['distance'] for t,b in zip(camera['target'],back))
    right = (math.cos(yaw),0.,-math.sin(yaw))
    up = (-math.sin(pitch)*math.sin(yaw),math.cos(pitch),-math.sin(pitch)*math.cos(yaw))
    return eye,right,up,tuple(-b for b in back)


def project(camera, rect, point):
    """Where a world point lands in the map rect (logical pixels, y up), or None in front of
    the renderer's near plane."""
    eye,right,up,forward = view_basis(camera)
    d = tuple(p-e for p,e in zip(point,eye))
    depth = sum(a*b for a,b in zip(d,forward))
    if depth <= max(.05,camera['distance']*.01):return None
    focal = 1/math.tan(math.radians(camera.get('fov',FOV))/2)
    x = focal*rect.height/max(1.,rect.width)*sum(a*b for a,b in zip(d,right))/depth
    y = focal*sum(a*b for a,b in zip(d,up))/depth
    return rect.x+(x+1)*rect.width/2,rect.y+(y+1)*rect.height/2


def axis_rate(camera, rect, anchor, height):
    """Screen pixels per block up the vertical through `anchor` (x, z) at `height`: a 2D vector
    at least MIN_RATE long, so a view from straight above still drags at a usable pace."""
    x,z = anchor
    a,b = project(camera,rect,(x,height-.5,z)),project(camera,rect,(x,height+.5,z))
    if a is None or b is None:return 0.,MIN_RATE
    vx,vy = b[0]-a[0],b[1]-a[1]
    length = math.hypot(vx,vy)
    if length < 1e-9:return 0.,MIN_RATE
    scale = max(1.,MIN_RATE/length)
    return vx*scale,vy*scale


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
        # The box starts at its own height instead of easing from an earlier area's.
        for key in ('miex.preview.box.low','miex.preview.box.high'):s.motion.get('_motion',{}).pop(key,None)


def status_text(s=None):
    """Blender's status bar while the 3D view shows; a handle under the pointer says what it does."""
    if s is not None and s.drag and s.drag[0] == 'y_handle':
        return f'Drag to set {HANDLES[s.drag[3]["prop"]]}, Esc or right-click to cancel'
    handle = handle_at(s,*s.hover) if s is not None and not s.drag else None
    if handle:return f'Drag up or down to set {HANDLES[handle[0]]}'
    return 'Drag to orbit, middle mouse or Shift-drag to pan, wheel to zoom, right-click for actions'


def handle_at(s, x, y):
    """The Max Y or Min Y handle under the pointer, as (prop, point, anchor), or None."""
    if not s.preview.on or s.menu or any(box.contains(x,y) for box in s.overlay_rects):return None
    near = [(math.hypot(point[0]-x,point[1]-y),handle) for handle in s.y_handles for point in (handle[1],)]
    near = [item for item in near if item[0] <= HANDLE_HIT]
    return min(near,key=lambda item:item[0])[1] if near else None


def grab(context, s, handle, x, y):
    """Press on a handle: its bound follows the pointer until release. False when locked."""
    from .world_exporter_canvas import action_locked, selection_snapshot, settle
    prop,_,anchor = handle
    if action_locked(s,('edit',prop)):return False
    settle(context,s)
    settings = context.scene.mcbedrock_world_exporter
    live = area_of(settings)[2:4]
    s.preview.hold()
    # The drag starts from the box's face as shown: a Max Y above the terrain starts at its top.
    low,high = s.preview.box_span(live)
    value = math.ceil(high-1e-3)-1 if prop == 'max_y' else math.floor(low+1e-3)
    limits = (min(WORLD_HEIGHT[0],*live),max(WORLD_HEIGHT[1],*live))
    s.drag = ('y_handle',(x,y),(x,y),dict(prop=prop,anchor=anchor,value=float(value),limits=limits,moved=False))
    s.drag_button = 'LEFTMOUSE'
    s.drag_snapshot = selection_snapshot(settings)
    return True


def drag_event(context, s, event, x, y):
    """A handle drag. The bound moves in whole blocks along the box's vertical axis as it
    shows on screen, within the world's height and never past the other bound. Release
    keeps it as one undo step; Esc or a right click puts it back."""
    from .world_exporter_canvas import action_locked, remember_selection, restore_selection, selection_snapshot
    settings = context.scene.mcbedrock_world_exporter
    kind,start,last,drag = s.drag
    prop = drag['prop']
    if (action_locked(s,('edit',prop)) or event.type == 'WINDOW_DEACTIVATE'
            or (event.type in {'ESC','RIGHTMOUSE'} and event.value == 'PRESS')):
        restore_selection(settings,s.drag_snapshot)
        s.drag = s.drag_snapshot = s.drag_button = None
        return
    if event.type in {'MOUSEMOVE','INBETWEEN_MOUSEMOVE'}:
        if not drag['moved'] and max(abs(x-start[0]),abs(y-start[1])) < 4:return
        drag['moved'] = True
        lowest,highest = drag['limits']
        if prop == 'max_y':lowest = max(lowest,settings.min_y)
        else:highest = min(highest,settings.max_y)
        # Short steps, each at the axis's own scale there, so perspective never lets the handle drift.
        dx,dy = x-last[0],y-last[1]
        steps = max(1,math.ceil(max(abs(dx),abs(dy))/2))
        lift = 1 if prop == 'max_y' else 0
        for _ in range(steps):
            rx,ry = axis_rate(s.preview.camera,s.map_rect,drag['anchor'],drag['value']+lift)
            drag['value'] = max(lowest,min(highest,drag['value']+(dx*rx+dy*ry)/steps/(rx*rx+ry*ry)))
        s.drag = (kind,start,(x,y),drag)
        value = math.floor(drag['value']+.5)
        if value != getattr(settings,prop):
            setattr(settings,prop,value)
            s.has_selection = True
            if settings.status.startswith(('Imported','Export saved')):settings.status = 'Selection ready'
    elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
        if selection_snapshot(settings) != s.drag_snapshot:remember_selection(s,s.drag_snapshot)
        s.drag = s.drag_snapshot = s.drag_button = None


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
        # A press on a Y handle drags it instead of orbiting.
        handle = handle_at(s,x,y) if event.type == 'LEFTMOUSE' and not getattr(event,'shift',False) else None
        if handle and grab(context,s,handle,x,y):return True
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
        lift = 0. if active else paint.wash(cell.inset((2,2,2,2)),command,enabled)
        ink = (1,1,1,1) if active else blend(T.text_muted,T.text,lift) if enabled else T.text_muted[:3]+(.6,)
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
    s.y_handles = []
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
        span = _box_span(s,preview,settings)
        lines = preview.selection_lines(r._color(T.accent[:3]+(.55,)),1.5*r.ui_scale,span)
        scene.render(box.width,box.height,preview.camera,lines,clear,preview.revision)
        r.push_clip(rect)
        try:
            scene.draw(r,rect,shown*r.opacity)
            opacity = r.opacity
            r.opacity = opacity*shown
            try:
                _draw_handles(paint,s,preview,settings,rect,span,T)
                _draw_notes(paint,preview,rect,T,fit_text)
            finally:r.opacity = opacity
        finally:r.pop_clip()
    elif not preview.on:
        # Fully back on the map: release the GPU copy, keep the decoded chunks.
        preview.free_gpu()
        preview.last_advance = None
    if loading_card(preview,rect) is not None:
        _draw_loading(paint,s,preview,rect,T,fit_text)


def _box_span(s, preview, settings):
    """The box's (bottom, top) as drawn, or None without one. It follows the fields at once and
    eases to new heights; the face being dragged sits on its value and eases on from there."""
    if preview.area is None or not preview.terrain:return None
    held = s.drag[3]['prop'] if s.drag and s.drag[0] == 'y_handle' else None
    low,high = preview.box_span(area_of(settings)[2:4],held)
    return (_follow(s.motion,'miex.preview.box.low',low,held == 'min_y'),
            _follow(s.motion,'miex.preview.box.high',high,held == 'max_y'))


def _follow(motion, key, target, direct):
    """Ease toward target; a direct value is shown as it is and eased from once let go."""
    if direct and not motion.get('_motion_disabled'):
        motion.setdefault('_motion',{})[key] = [target,target,motion.get('_motion_time',time.monotonic()),BOX_EASE]
        return target
    return tween(motion,key,target,BOX_EASE)


def _draw_handles(paint, s, preview, settings, rect, span, T):
    """The Max Y and Min Y dots on the box, white with a Sparkle Blue edge like the map's
    selection handles. The one under the pointer or being dragged grows, and a drag shows
    its value beside it. Locked controls (an export running) mute them and take no drags."""
    if span is None:return
    from .world_exporter_canvas import action_locked
    r = paint.r
    drag = s.drag[3] if s.drag and s.drag[0] == 'y_handle' else None
    enabled = not action_locked(s,('edit','max_y'))
    card = loading_card(preview,rect)
    for prop,point,anchor in preview.handles(preview.camera,rect,span,drag['anchor'] if drag else None):
        if not rect.contains(*point) or (card is not None and card.contains(*point)):continue
        held = drag is not None and drag['prop'] == prop
        near = (enabled and not s.drag and not s.menu and preview.on
                and math.hypot(point[0]-s.hover[0],point[1]-s.hover[1]) <= HANDLE_HIT)
        size = HANDLE+2*paint.ease('handle.'+prop,held or near,.12)
        r.rect(Rect(point[0]-size/2,point[1]-size/2,size,size),T.text if enabled else T.text_muted,
               radius=size/2,border=T.accent,border_width=1.5)
        if enabled and preview.on:s.y_handles.append((prop,point,anchor))
        if held:_draw_value(paint,rect,point,size,f'{HANDLES[prop]} {getattr(settings,prop):,}')


def _draw_value(paint, rect, point, size, text):
    """The dragged bound beside its handle: white SemiBold with the size label's soft shadow,
    on the left when the right would leave the map."""
    from .world_exporter_layout import text_width
    r = paint.r
    width = text_width(r,text,12,'SemiBold')+2
    x = point[0]+size/2+8
    if x+width > rect.right-8:x = point[0]-size/2-8-width
    label = Rect(x,point[1]-9,width,18)
    r.text(Rect(label.x+1,label.y-1,label.width,label.height),text,(0,0,0,.45),size=12,weight='SemiBold')
    r.text(label,text,(1,1,1,1),size=12,weight='SemiBold')


def loading_card(preview, rect):
    """The bottom-centred card that counts chunks while they load, or None."""
    if not preview.on or preview.complete() or preview.too_large or preview.failed:return None
    width = min(300,max(0,rect.width-32))
    if width < 120:return None
    return Rect(rect.x+(rect.width-width)/2,rect.y+16,width,56)


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
    card = loading_card(preview,rect)
    if card is None:return
    width = card.width
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
