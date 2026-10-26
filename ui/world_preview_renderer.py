"""GPU drawing for the World Exporter's 3D preview.

Terrain renders into an offscreen with a depth buffer, supersampled 2x when
the map is small enough, and is then blitted into the map with a box filter.
The offscreen is redrawn only when the camera, the meshes or the size change.
"""
import math
import numpy as np
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

_terrain = None
_blit = None
# Offscreens above this many physical pixels skip supersampling.
SUPERSAMPLE_PIXELS = 2_400_000
# Arrival batches are folded into one draw once there are this many
# (a full-budget preview arrives in about 25 of them).
MAX_BATCHES = 32


def terrain_shader():
    global _terrain
    if _terrain is not None:return _terrain
    info=gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4','ModelViewProjectionMatrix')
    info.vertex_in(0,'VEC3','pos');info.vertex_in(1,'VEC3','color')
    interface=gpu.types.GPUStageInterfaceInfo('mcaddon_preview_color')
    interface.smooth('VEC3','shade');info.vertex_out(interface)
    info.fragment_out(0,'VEC4','fragColor')
    info.vertex_source('void main(){shade=color;gl_Position=ModelViewProjectionMatrix*vec4(pos,1.0);}')
    info.fragment_source('void main(){fragColor=vec4(shade,1.0);}')
    _terrain=gpu.shader.create_from_info(info)
    return _terrain


def blit_shader():
    global _blit
    if _blit is not None:return _blit
    info=gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4','ModelViewProjectionMatrix')
    info.push_constant('FLOAT','opacity')
    info.push_constant('INT','factor')
    info.vertex_in(0,'VEC2','pos');info.vertex_in(1,'VEC2','texCoord')
    interface=gpu.types.GPUStageInterfaceInfo('mcaddon_preview_uv')
    interface.smooth('VEC2','uv');info.vertex_out(interface)
    info.sampler(0,'FLOAT_2D','image')
    info.fragment_out(0,'VEC4','fragColor')
    info.vertex_source('void main(){uv=texCoord;gl_Position=ModelViewProjectionMatrix*vec4(pos,0.0,1.0);}')
    # A supersampled offscreen holds factor x factor texels per map pixel; average them.
    info.fragment_source('''
        void main(){
            ivec2 size=textureSize(image,0);
            ivec2 pixel=clamp(ivec2(floor(uv*vec2(size/factor))),ivec2(0),size/factor-ivec2(1));
            vec3 sum=vec3(0.0);
            for(int y=0;y<factor;y++)for(int x=0;x<factor;x++)sum+=texelFetch(image,pixel*factor+ivec2(x,y),0).rgb;
            fragColor=vec4(sum/float(factor*factor),opacity);
        }
    ''')
    _blit=gpu.shader.create_from_info(info)
    return _blit


def look_at(eye, target):
    """Right-handed view matrix with world Y up (Minecraft: X east, Z south)."""
    forward=(target-eye).normalized()
    side=forward.cross(Vector((0,1,0)))
    if side.length<1e-6:side=Vector((1,0,0))
    side.normalize()
    up=side.cross(forward)
    return Matrix(((*side,-side.dot(eye)),(*up,-up.dot(eye)),(*-forward,forward.dot(eye)),(0,0,0,1)))


def perspective(fov, aspect, near, far):
    f=1/math.tan(fov/2)
    return Matrix(((f/max(aspect,1e-6),0,0,0),(0,f,0,0),(0,0,(far+near)/(near-far),2*far*near/(near-far)),(0,0,-1,0)))


def camera_matrices(camera, aspect):
    """(projection, view, eye) for an orbit camera dict: yaw/pitch in degrees, distance, target."""
    yaw=math.radians(camera['yaw']);pitch=math.radians(camera['pitch'])
    target=Vector(camera['target'])
    offset=Vector((math.cos(pitch)*math.sin(yaw),math.sin(pitch),math.cos(pitch)*math.cos(yaw)))
    eye=target+offset*camera['distance']
    reach=camera.get('radius',64)
    near=max(.05,camera['distance']*.01)
    far=camera['distance']+reach*4+64
    return perspective(math.radians(camera['fov']),aspect,near,far),look_at(eye,target),eye


class Scene:
    """GPU side of one preview: batches per arrival, the offscreen and its last render."""
    def __init__(self):
        self.batches=[]      # (chunk keys, GPUBatch, {key: decoded arrays it was built from})
        self.offscreen=None
        self.factor=1
        self.stamp=None
        self.synced=None

    def free(self):
        self.batches=[];self.offscreen=None;self.stamp=None;self.synced=None

    def sync(self, meshes, wanted, revision):
        """Upload what changed since the last sync.

        `meshes` maps chunk keys to decoded arrays (or None for empty chunks);
        `wanted` holds the keys the current area shows. A batch whose chunks all
        still show their uploaded arrays is kept; the others are rebuilt with the
        new arrivals, and everything folds into one batch when they pile up.
        Nothing runs until the mesh revision changes.
        """
        if revision==self.synced:return False
        self.synced=revision
        current={key:meshes[key] for key in wanted if meshes.get(key) is not None}
        kept=[];placed=set()
        for keys,batch,members in self.batches:
            if all(current.get(key) is mesh for key,mesh in members.items()):
                kept.append((keys,batch,members));placed|=keys
        changed=len(kept)!=len(self.batches)
        self.batches=kept
        fresh=[key for key in current if key not in placed]
        if fresh and len(self.batches)>=MAX_BATCHES:
            self.batches=[];fresh=list(current)
        if fresh:
            batch=_batch([current[key] for key in fresh])
            if batch is not None:self.batches.append((frozenset(fresh),batch,{key:current[key] for key in fresh}))
        return changed or bool(fresh)

    def render(self, width, height, camera, lines, clear, revision):
        """Draw into the offscreen when anything changed; returns the factor used."""
        width=max(1,int(round(width)));height=max(1,int(round(height)))
        factor=2 if width*height<=SUPERSAMPLE_PIXELS else 1
        size=(width*factor,height*factor)
        if self.offscreen is None or (self.offscreen.width,self.offscreen.height)!=size:
            try:self.offscreen=gpu.types.GPUOffScreen(*size,format='RGBA16F')
            except (ValueError,RuntimeError):
                factor=1;size=(width,height)
                self.offscreen=gpu.types.GPUOffScreen(*size,format='RGBA16F')
            self.stamp=None
        self.factor=factor
        key=(tuple(sorted((k,v if not isinstance(v,list) else tuple(v)) for k,v in camera.items())),
             size,revision,tuple(id(batch) for _,batch,_ in self.batches),tuple(clear),lines)
        if key==self.stamp:return factor
        self.stamp=key
        projection,view,_=camera_matrices(camera,width/height)
        # Called before the map clip is pushed; the offscreen has no scissor.
        gpu.state.scissor_test_set(False)
        with self.offscreen.bind():
            framebuffer=gpu.state.active_framebuffer_get()
            gpu.state.depth_mask_set(True)
            framebuffer.clear(color=clear,depth=1.0)
            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.blend_set('NONE')
            # MiEx winds every quad counter-clockwise around its normal.
            gpu.state.face_culling_set('BACK')
            shader=terrain_shader()
            shader.bind()
            shader.uniform_float('ModelViewProjectionMatrix',projection@view)
            for _,batch,_ in self.batches:batch.draw(shader)
            gpu.state.face_culling_set('NONE')
            if lines:
                _draw_lines(lines,projection,view,size,factor)
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            gpu.state.blend_set('ALPHA')
        return factor

    def draw(self, renderer, rect, opacity):
        """Blit the last render into rect (logical pixels) at the given opacity."""
        if self.offscreen is None or opacity<=0:return
        box=renderer._physical_rect(rect)
        shader=blit_shader()
        shader.bind()
        shader.uniform_float('ModelViewProjectionMatrix',renderer._matrix)
        shader.uniform_float('opacity',float(opacity))
        shader.uniform_int('factor',self.factor)
        shader.uniform_sampler('image',self.offscreen.texture_color)
        gpu.state.blend_set('ALPHA')
        batch_for_shader(shader,'TRI_FAN',{'pos':[(box.x,box.y),(box.right,box.y),(box.right,box.top),(box.x,box.top)],
            'texCoord':[(0,0),(1,0),(1,1),(0,1)]}).draw(shader)


def _batch(parts):
    parts=[p for p in parts if p is not None]
    if not parts:return None
    offsets=np.cumsum([0]+[len(p[0]) for p in parts[:-1]])
    positions=np.ascontiguousarray(np.concatenate([p[0] for p in parts]),dtype=np.float32)
    colors=np.ascontiguousarray(np.concatenate([p[1] for p in parts]),dtype=np.float32)
    triangles=np.ascontiguousarray(np.concatenate([p[2]+np.int32(o) for p,o in zip(parts,offsets)]),dtype=np.int32)
    return batch_for_shader(terrain_shader(),'TRIS',{'pos':positions,'color':colors},indices=triangles)


def _draw_lines(lines, projection, view, size, factor):
    """Selection edges in a polyline shader, hidden behind terrain by the depth test."""
    segments,color,width=lines
    shader=gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    with gpu.matrix.push_pop():
        gpu.matrix.load_matrix(view)
        with gpu.matrix.push_pop_projection():
            gpu.matrix.load_projection_matrix(projection)
            gpu.state.blend_set('ALPHA')
            shader.bind()
            shader.uniform_float('viewportSize',size)
            shader.uniform_float('lineWidth',width*factor)
            shader.uniform_float('color',color)
            batch_for_shader(shader,'LINES',{'pos':segments}).draw(shader)


def reset():
    global _terrain,_blit
    _terrain=_blit=None
