"""Self-contained sky sprites with optional resource-pack artwork."""
import os
import tempfile
import zipfile
from pathlib import Path
import bpy

BUNDLED = Path(__file__).resolve().parents[1] / 'assets/sky'
ARTWORK_VERSION = 3

PATHS = [('assets/minecraft/textures/environment/sun.png','assets/minecraft/textures/environment/moon_phases.png'),
         ('textures/environment/sun.png','textures/environment/moon_phases.png')]
PHASES = [('FULL','Full',0),('WANING_GIBBOUS','Waning ¾',1),('HALF','Waning ½',2),('CRESCENT','Waning ¼',3),
          ('NEW','New',4),('WAXING_CRESCENT','Waxing ¼',5),('FIRST_QUARTER','Waxing ½',6),('WAXING_GIBBOUS','Waxing ¾',7)]


def resolve(path):
    path=Path(path)
    if path.is_file() and path.suffix.lower() not in {'.zip','.mcpack'}:
        path=next((p for p in path.parents if (p/'pack.mcmeta').exists() or (p/'manifest.json').exists()),path.parent)
    if path.is_dir():
        for sun,moon in PATHS:
            if (path/sun).is_file() and (path/moon).is_file():
                return str(path),sun,moon
    elif path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names=archive.namelist()
            for sun,moon in PATHS:
                for name in names:
                    if name.endswith(sun):
                        prefix=name[:-len(sun)]
                        if prefix+moon in names:
                            return str(path),name,prefix+moon
    return None


def discover(scene):
    """Use an explicitly selected pack; otherwise use built-in artwork.

    Missing packs in older projects must not prevent creating a new sky.
    Existing skies retain their packed images.
    """
    explicit=scene.mcbedrock_sky.source_pack
    return resolve(bpy.path.abspath(explicit)) if explicit else None


def builtin_pixels(name,width,height,version=2):
    pixels=[]
    for y in range(height):
        for x in range(width):
            u,v=x%16,y%16
            # The sky projection is calibrated for a sprite occupying one
            # quarter of each texture cell. The first built-ins filled half.
            visible=(4<=u<12 and 4<=v<12) if version==1 else (6<=u<10 and 6<=v<10)
            if name=='Sun':
                center=(5<=u<11 and 5<=v<11) if version==1 else (7<=u<9 and 7<=v<9)
                color=(1.0,.94,.76) if center else (1.0,.78,.40)
            else:
                phase=(1-y//16)*4+x//16
                lit=((True,u<10,u<8,u<6,False,u>=10,u>=8,u>=6) if version==1
                     else (True,u<9,u<8,u<7,False,u>=9,u>=8,u>=7))[phase]
                visible=visible and lit
                shade=.65 if (u,v) in {(6,9),(7,9),(6,8),(9,6)} else .9
                color=(shade,shade,shade)
            pixels.extend((*color,1) if visible else (0,0,0,0))
    return pixels


def builtin_images(owner):
    """Load the licensed Bare Bones artwork without requiring a resource pack."""
    return [bundled_image(name,owner) for name in ('Sun','Moon')]


def bundled_image(name,owner):
    filename='sun.png' if name=='Sun' else 'moon_phases.png'
    image=bpy.data.images.load(str(BUNDLED/filename),check_existing=False)
    image.name='MC Sky '+name
    image['mc_sky_owner']=owner
    image['mc_sky_builtin_version']=ARTWORK_VERSION
    image.pack()
    return image


def upgrade_builtin_images(images,owner):
    """Replace only untouched generated artwork, including saved beta skies."""
    for index,(image,name,size) in enumerate(zip(images,('Sun','Moon'),((16,16),(64,32)))):
        if (image.library or image.get('mc_sky_builtin_version',0)>=ARTWORK_VERSION
                or image.get('mc_sky_owner')!=owner or image.filepath
                or not image.name.startswith('MC Sky '+name) or tuple(image.size)!=size):
            continue
        # Saved byte images can differ by one quantization step. An edited
        # texture with different pixels is deliberately left alone.
        pixels=image.pixels[:]
        if not any(all(abs(a-b)<=1/255+.00001 for a,b in zip(pixels,builtin_pixels(name,*size,version=version)))
                   for version in (1,2)):
            continue
        replacement=bundled_image(name,owner)
        image.user_remap(replacement)
        images[index]=replacement
        if not image.users:bpy.data.images.remove(image)


def load_images(source,owner):
    if source is None:
        return builtin_images(owner)
    root,sun,moon=source
    images=[]
    try:
        if Path(root).is_dir():
            for name in (sun,moon):
                images.append(bpy.data.images.load(str(Path(root)/name),check_existing=False))
        else:
            with zipfile.ZipFile(root) as archive:
                for index,name in enumerate((sun,moon)):
                    if archive.getinfo(name).file_size>16*1024*1024:
                        raise ValueError('Sky texture is too large')
                    handle,path=tempfile.mkstemp(prefix='mc_sky_',suffix='.png')
                    try:
                        with os.fdopen(handle,'wb') as stream:
                            stream.write(archive.read(name))
                        image=bpy.data.images.load(path,check_existing=False)
                        images.append(image)
                        image.pack()
                    finally:
                        os.unlink(path)
        if images[0].size[0]!=images[0].size[1] or images[1].size[0]*2!=images[1].size[1]*4:
            raise ValueError('Expected a square sun and a 4 by 2 moon atlas')
        for image in images:
            image['mc_sky_owner']=owner
            image.pack()
        return images
    except Exception:
        for image in images:
            if not image.users:
                bpy.data.images.remove(image)
        raise
