"""Native LOOCS 0.12 colour renderings, log views and texture-correction spaces.

Setup extends a copy of Blender's own OCIO config (never the installation):
every LOOCS SDR colour rendering becomes a view on the sRGB display, LOOCS and
camera log encodings become views, and Bram's "sRGB TC <Rendering>" texture
correction input spaces become image colour spaces. Transforms are read from
LOOCS's own reduced config; LUTs are expanded from bundled compressed data.
"""
import copy
import json
import os
import shutil
import uuid
from functools import lru_cache
from pathlib import Path

import bpy
from .compatibility import SkyOperator

ASSETS = Path(__file__).resolve().parents[1] / 'assets/loocs'
LOOCS_CONFIG = ASSETS / 'loocs_config.ocio'
BACKUP = 'sparkle-original-config.ocio'
MARKER = 'Sparkle LOOCS profile v3'
# v2 showed LOOCS views on their own display colour space, which the viewport skips.
PREVIOUS_MARKER = 'Sparkle LOOCS profile v2'
LEGACY_MARKER = 'Sparkle ChromaLuma profile v1'
LUT_DIR = 'sparkle_loocs'
DISPLAY = 'sRGB'
PREFIX = 'LOOCS '

# (property id, LOOCS view name). Chroma Luma is Sparkle's default.
RENDERINGS = (
    ('CHROMA_LUMA', 'Chroma Luma'), ('FILMIC', 'Filmic'), ('FILMIC_CLASSIC', 'Filmic Classic'),
    ('FILMIC_CLASSIC_BW', 'Filmic Classic BW'), ('FILMIC_VINTAGE', 'Filmic Vintage'),
    ('NEUTRAL', 'Neutral'), ('ANIMATION', 'Animation'), ('FLAT', 'Flat'), ('HVS', 'HVS'),
    ('PAINTER', 'Painter'))
RENDERING_LABELS = dict(RENDERINGS)
# LOOCS ships texture corrections for these renderings. Black-and-white shares
# Filmic Classic's tone curve; Painter has no correction.
CORRECTIONS = ('CHROMA_LUMA', 'FILMIC', 'FILMIC_CLASSIC', 'FILMIC_VINTAGE', 'NEUTRAL', 'ANIMATION', 'FLAT', 'HVS')
# Sparkle's own inverse of Chroma Luma (built for LOOCS 0.11, whose Chroma Luma
# matches 0.12's within a few 8-bit steps), applied by shader nodes to the
# final surface color, tint included, and to a quarter of the daylight sky.
# It is what Match Rendering gives Chroma Luma: the colors Sparkle had before
# LOOCS 0.12. Bram's 0.12 Chroma Luma correction darkened MiEx worlds by about
# a fifth and greyed the sky; it stays available by name.
CLASSIC = 'CLASSIC'
CORRECTION_FOR = {**{key: key for key in CORRECTIONS}, 'FILMIC_CLASSIC_BW': 'FILMIC_CLASSIC', 'CHROMA_LUMA': CLASSIC}
# Log encodings LOOCS's DCTLs accept as input. Blender's own definitions are
# used where they exist; LOOCS's are imported otherwise.
LOGS = (('LOOCS_LOG', 'LOOCS Log', ('LOOCS Log',)),
        ('ARRI_LOGC3', 'ARRI LogC3', ('ARRI LogC3 (EI800)', 'ARRI LogC3')),
        ('ARRI_LOGC4', 'ARRI LogC4', ('ARRI LogC4',)),
        ('SLOG3', 'S-Log3', ('S-Log3 S-Gamut3',)),
        ('SLOG3_CINE', 'S-Log3 Cine', ('S-Log3 S-Gamut3.Cine',)),
        ('VLOG', 'V-Log', ('V-Log V-Gamut',)),
        ('RED_LOG3G10', 'RED Log3G10', ('Log3G10 REDWideGamutRGB',)),
        ('RED_LOGFILM', 'REDLogFilm', ('REDLogFilm REDWideGamutRGB',)))
LOG_LABELS = {key: label for key, label, _ in LOGS}

# Kept so the existing tests and review scripts name the default view.
VIEW = PREFIX + 'Chroma Luma'
LEGACY_VIEW = 'Sparkle ChromaLuma'


def rendering_view(key):
    return PREFIX + RENDERING_LABELS[key]


def log_view(key):
    return 'Log · ' + LOG_LABELS[key]


def correction_space(key):
    return 'sRGB TC ' + RENDERING_LABELS[key]


def correction_label(key):
    return 'Sparkle classic' if key == CLASSIC else RENDERING_LABELS[key]


RENDERING_VIEWS = {rendering_view(key): key for key, _ in RENDERINGS}
LOG_VIEWS = {log_view(key): key for key, _, _ in LOGS}


def working_space():
    return getattr(getattr(bpy.data, 'colorspace', None), 'working_space', 'Linear Rec.709')


@lru_cache(maxsize=16)
def _linear_matrix(source, destination, config_id):
    import PyOpenColorIO as ocio
    processor = ocio.GetCurrentConfig().getProcessor(source, destination).getDefaultCPUProcessor()
    columns = [processor.applyRGB([float(i == j) for i in range(3)]) for j in range(3)]
    return tuple(tuple(columns[j][i] for j in range(3)) for i in range(3))


def linear_matrix(source, destination):
    """OCIO linear-primary conversion, including white-point adaptation."""
    import PyOpenColorIO as ocio
    return _linear_matrix(source, destination, ocio.GetCurrentConfig().getCacheID())


def _views(scene):
    # Dynamic RNA enum items are empty when inspected through bl_rna.
    # Asking OCIO for views also works before any sky exists.
    try:
        import PyOpenColorIO as ocio
        return list(ocio.GetCurrentConfig().getViews(scene.display_settings.display_device))
    except Exception:
        return []


def available(scene):
    return VIEW in _views(scene)


def available_logs(scene):
    views = set(_views(scene))
    return [key for key, _, _ in LOGS if log_view(key) in views]


def installed_version():
    """3 for the current LOOCS 0.12 profile, 2 for the one whose views the
    viewport showed too saturated, 1 for the old ChromaLuma-only one, else 0."""
    try:
        import PyOpenColorIO as ocio
        description = ocio.GetCurrentConfig().getDescription()
    except Exception:
        return 0
    return 3 if MARKER in description else 2 if PREVIOUS_MARKER in description else 1 if LEGACY_MARKER in description else 0


def current_rendering(scene):
    return RENDERING_VIEWS.get(scene.view_settings.view_transform)


def active(scene):
    """A LOOCS colour rendering is the scene's display view."""
    return scene.view_settings.view_transform in RENDERING_VIEWS


def wants_loocs(scene):
    """Shown through LOOCS, including the 0.11 view awaiting the profile update."""
    return active(scene) or scene.view_settings.view_transform == LEGACY_VIEW


def correction(scene):
    """Texture correction matching the scene, or None."""
    s = scene.mcbedrock_sky
    choice = s.loocs_texture_correction
    if choice == 'NONE':
        return None
    if choice != 'MATCH':
        return choice
    return CORRECTION_FOR.get(current_rendering(scene) or s.loocs_rendering)


def textures_handled(scene):
    """Image colour spaces carry the correction, so shaders must not repeat it.
    Sparkle classic has no colour space: shaders always apply it."""
    key = correction(scene)
    return bool(scene.mcbedrock_sky.loocs_auto_textures and key and key != CLASSIC and
                _has_space(correction_space(key)))


def compensation_amount(scene):
    s = scene.mcbedrock_sky
    if textures_handled(scene):
        return 0
    shown = active(scene) or scene.view_settings.view_transform in LOG_VIEWS
    return s.texture_compensation if shown and correction(scene) else 0


def _has_space(name):
    try:
        import PyOpenColorIO as ocio
        return ocio.GetCurrentConfig().getColorSpace(name) is not None
    except Exception:
        return False


TC_TAG = 'mc_loocs_texture_correction'
_texture_signature = [None]


def texture_target(scene):
    key = correction(scene)
    # Log footage is graded with the chosen rendering later, so keep its correction.
    shown = active(scene) or scene.view_settings.view_transform in LOG_VIEWS
    if scene.mcbedrock_sky.loocs_auto_textures and shown and key and key != CLASSIC:
        return correction_space(key)
    return 'sRGB'


def sync_textures(scene, force=False):
    """Give material colour textures the "sRGB TC" space of the active rendering.

    Only plain sRGB colour textures change, and Sparkle tags the ones it
    switched so they follow later rendering changes and return to sRGB. A
    texture whose colour space the user picked is theirs. Sky artwork and
    Sparkle's data images are never corrected."""
    target = texture_target(scene)
    signature = (len(bpy.data.images), len(bpy.data.materials), target)
    if not force and signature == _texture_signature[0]:
        return 0
    _texture_signature[0] = signature
    if target != 'sRGB' and not _has_space(target):
        return 0
    spaces = {correction_space(key) for key in CORRECTIONS}
    images = {image.as_pointer(): image for image in bpy.data.images if image.get(TC_TAG)}
    if target != 'sRGB':
        seen = set()
        for material in bpy.data.materials:
            # Sky-owned materials: its artwork, and copies of materials scanned here.
            if material.get('mc_sky_owner'):
                continue
            for image in _color_images(material, seen):
                if not (image.get('mc_sky_owner') or image.get('mc_lighting_asset')):
                    images.setdefault(image.as_pointer(), image)
    changed = 0
    for image in images.values():
        if image.library:
            continue
        name = image.colorspace_settings.name
        if image.get(TC_TAG) and name not in spaces and name != 'sRGB':
            image.pop(TC_TAG, None)     # the user picked another colour space
            continue
        if name == target or (not image.get(TC_TAG) and name != 'sRGB'):
            continue
        image.colorspace_settings.name = target
        if target == 'sRGB':
            image.pop(TC_TAG, None)
        else:
            image[TC_TAG] = True
        changed += 1
    return changed


def _watch_textures():
    context = bpy.context
    scene = getattr(getattr(context, 'window', None), 'scene', None) or getattr(context, 'scene', None)
    if scene is not None and hasattr(scene, 'mcbedrock_sky'):
        try:
            sync_textures(scene)
        except Exception:
            pass
    return 1.0


def register_handlers():
    if not bpy.app.timers.is_registered(_watch_textures):
        bpy.app.timers.register(_watch_textures, first_interval=1, persistent=True)


def unregister_handlers():
    if bpy.app.timers.is_registered(_watch_textures):
        bpy.app.timers.unregister(_watch_textures)
    _texture_signature[0] = None


# Derived settings for the native Color Management panel: they read and write
# the scene's view transform, so Blender's own View menu stays the truth.
OUTPUTS = (('DISPLAY', 'Display', 'Show the color rendering, for viewing and final images'),
           *[(key, label, f'Render flat {label} footage for grading with the LOOCS input DCTL')
             for key, label, _ in LOGS])


def get_enabled(settings):
    scene = settings.id_data
    return active(scene) or scene.view_settings.view_transform in LOG_VIEWS


def set_enabled(settings, value):
    scene = settings.id_data
    select(scene, value)
    correction_changed(scene)


def get_output(settings):
    key = LOG_VIEWS.get(settings.id_data.view_settings.view_transform)
    return 0 if key is None else 1 + [k for k, _, _ in LOGS].index(key)


def set_output(settings, index):
    scene = settings.id_data
    if index == 0:
        select(scene, True)
    else:
        select_log(scene, LOGS[index - 1][0])
    correction_changed(scene)


def rendering_changed(scene):
    """The sidebar picked a rendering: show it and follow with the textures."""
    if available(scene):
        select(scene, True)
    correction_changed(scene)


def correction_changed(scene):
    sync_textures(scene, force=True)
    s = scene.mcbedrock_sky
    if s.active:
        from . import lighting_pipeline
        lighting_pipeline.update(scene)


def select(scene, enabled, rendering=None):
    s = scene.mcbedrock_sky
    if enabled:
        if not available(scene):
            return False
        view = rendering_view(rendering or s.loocs_rendering)
        if view not in _views(scene):
            view = VIEW
        if not wants_loocs(scene) and scene.view_settings.view_transform not in LOG_VIEWS:
            s.native_previous_view = scene.view_settings.view_transform
            s.native_previous_look = scene.view_settings.look
        if scene.view_settings.view_transform != view:
            scene.view_settings.view_transform = view
    elif wants_loocs(scene) or scene.view_settings.view_transform in LOG_VIEWS:
        previous = s.native_previous_view
        if previous in RENDERING_VIEWS or previous in LOG_VIEWS or previous == LEGACY_VIEW:
            previous = ''
        scene.view_settings.view_transform = previous or 'AgX'
        try:
            scene.view_settings.look = s.native_previous_look or 'None'
        except TypeError:
            pass
    s['loocs_enabled'] = wants_loocs(scene)
    return True


def select_log(scene, key):
    s = scene.mcbedrock_sky
    view = log_view(key)
    if view not in _views(scene):
        return False
    if not active(scene) and scene.view_settings.view_transform not in LOG_VIEWS:
        s.native_previous_view = scene.view_settings.view_transform
        s.native_previous_look = scene.view_settings.look
    scene.view_settings.view_transform = view
    s['loocs_enabled'] = False
    return True


def migrate(scene):
    """Retire display overrides once, preserving any subsequent user edits."""
    s = scene.mcbedrock_sky
    if not s.active or s.native_color_version:
        return
    view = scene.view_settings
    # Only undo the exact display state imposed by the old compositor pipeline.
    managed = (view.view_transform == 'Standard' and view.look == 'None'
            and view.exposure == 0 and view.gamma == 1
            and not view.use_curve_mapping and not view.use_white_balance)
    if managed:
        view.view_transform = s.previous_view or 'AgX'
        try:
            view.look = s.previous_look or 'None'
        except TypeError:
            pass
        view.exposure = s.previous_exposure
        for key, value in json.loads(s.previous_display_settings or '{}').items():
            target = scene.display_settings if key == 'display_device' else view
            setattr(target, key, value)
    s.native_color_version = 1
    if managed and available(scene) and s.loocs_enabled:
        select(scene, True)


def migrate_profile(scene):
    """Scenes saved with the 0.11 ChromaLuma view move to LOOCS 0.12 Chroma Luma.

    Blender drops a view missing from the loaded config, so the stored LOOCS
    choice, not the view name, says whether the scene used ChromaLuma."""
    s = scene.mcbedrock_sky
    if s.get('loocs_profile_version', 0) >= 2 or not available(scene):
        return
    s['loocs_profile_version'] = 2
    if s.active and s.loocs_enabled and not active(scene):
        select(scene, True)


def sync(scene):
    """Read the user's display choice; never reset it from an update handler."""
    s = scene.mcbedrock_sky
    migrate(scene)
    migrate_profile(scene)
    if s.native_color_pending and available(scene):
        select(scene, True)
        s.native_color_pending = False
    if s.loocs_enabled != wants_loocs(scene):s['loocs_enabled'] = wants_loocs(scene)
    rendering = current_rendering(scene)
    if rendering and rendering != s.loocs_rendering:
        # Picked from Blender's own View Transform menu.
        s['loocs_rendering'] = [key for key, _ in RENDERINGS].index(rendering)
    if s.exposure != scene.view_settings.exposure:s['exposure'] = scene.view_settings.exposure
    sync_textures(scene)
    # A user may change the native view without changing any sky properties.
    # Update the world compensation too, so the next timer tick matches it.
    if s.world:
        fallback=s.world.node_tree.nodes.get('Standard sky highlights')
        if fallback:
            amount=float(scene.view_settings.view_transform=='Standard')
            if fallback.inputs[0].default_value!=amount:
                fallback.inputs[0].default_value=amount
                # Distant clouds use a copy of this display branch. Updating
                # both together avoids bright seams when switching views.
                for group in bpy.data.node_groups:
                    if group.get('mc_sky_owner')==s.owner and group.get('mc_cloud_background'):
                        clone=group.nodes.get('Standard sky highlights')
                        if clone:clone.inputs[0].default_value=amount
        node=s.world.node_tree.nodes.get('Sky colour compensation')
        if node:
            from .sky import time_state, current_time, smoothstep
            direction=time_state(current_time(scene),s.azimuth)[0]
            amount=.25*smoothstep(.15,.55,direction.z) if active(scene) and correction(scene) else 0
            if node.inputs['Amount'].default_value!=amount:
                node.inputs['Amount'].default_value=amount
                for group in bpy.data.node_groups:
                    if group.get('mc_sky_owner')==s.owner and group.get('mc_cloud_background'):
                        clone=group.nodes.get('Sky colour compensation')
                        if clone:clone.inputs['Amount'].default_value=amount


# --- bundled LUT data -------------------------------------------------------

def correction_key_name(key):
    return RENDERING_LABELS[key].replace(' ', '_')


def correction_table(key):
    """The 9^3 sRGB texture-correction LUT, in .cube order (red fastest)."""
    import numpy as np
    with np.load(ASSETS / 'texture_correction.npz', allow_pickle=False) as data:
        return np.asarray(data[correction_key_name(key)], dtype=np.float32)


def _lut_rows(source_name):
    """Rows for one LOOCS LUT file name, from the bundled archives."""
    import numpy as np
    stem = Path(source_name).stem
    if stem.startswith('texture_correction_sRGB_'):
        archive, key = 'texture_correction.npz', stem[len('texture_correction_sRGB_'):]
    else:
        archive, key = 'renderings.npz', stem
    with np.load(ASSETS / archive, allow_pickle=False) as data:
        if key not in data.files:
            raise ValueError(f'LOOCS LUT {source_name} is not bundled')
        return np.asarray(data[key], dtype=np.float32)


def write_cube(source_name, destination):
    import numpy as np
    rows = _lut_rows(source_name)
    size = round(len(rows) ** (1 / 3))
    with open(destination, 'w', encoding='ascii', newline='\n') as handle:
        handle.write(f'TITLE "{Path(source_name).stem}"\nLUT_3D_SIZE {size}\n'
                     'DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n')
        np.savetxt(handle, rows, fmt='%.7g')


# --- profile construction ---------------------------------------------------

def _renamed(transform, files):
    """Copy a LOOCS transform, prefixing its LUT names to avoid collisions."""
    import PyOpenColorIO as ocio
    if isinstance(transform, ocio.GroupTransform):
        return ocio.GroupTransform([_renamed(child, files) for child in transform])
    if isinstance(transform, ocio.FileTransform):
        files.add(transform.getSrc())
        return ocio.FileTransform(src='loocs_' + transform.getSrc(), interpolation=transform.getInterpolation(),
                                  direction=transform.getDirection())
    return copy.deepcopy(transform)


def _loocs_definitions(loocs):
    import PyOpenColorIO as ocio
    to_709 = loocs.getColorSpace('sRGB-linear').getTransform(ocio.COLORSPACE_DIR_FROM_REFERENCE)
    from_709 = copy.deepcopy(to_709)
    from_709.setDirection(ocio.TRANSFORM_DIR_INVERSE)
    return to_709, from_709


def build_profile(config, lut_path):
    """Add LOOCS views and colour spaces to an OCIO config; return LUT names used."""
    import PyOpenColorIO as ocio
    loocs = ocio.Config.CreateFromFile(str(LOOCS_CONFIG))
    _, from_709 = _loocs_definitions(loocs)
    files = set()
    reference_to_709 = ocio.ColorSpaceTransform(src=ocio.ROLE_REFERENCE, dst='Linear Rec.709')

    def scene_space(name, loocs_name, family, encoding, description):
        if config.getColorSpace(name):
            return
        space = ocio.ColorSpace(ocio.REFERENCE_SPACE_SCENE, name=name, family=family, encoding=encoding,
                                description=description)
        source = loocs.getColorSpace(loocs_name).getTransform(ocio.COLORSPACE_DIR_FROM_REFERENCE)
        space.setTransform(ocio.GroupTransform([copy.deepcopy(reference_to_709), copy.deepcopy(from_709),
                                                _renamed(source, files)]),
                           ocio.COLORSPACE_DIR_FROM_REFERENCE)
        config.addColorSpace(space)

    # LOOCS's display gamut mapping lives inside each view, which then shows on
    # Blender's own sRGB display colour space (undone again at the end). As a
    # separate display colour space it was applied by F12 renders but skipped
    # by the GPU viewport, so Rendered view showed the daylight sky as pure,
    # electric blue while the render was a natural blue.
    display_space = config.getDisplayViewColorSpaceName(DISPLAY, config.getDefaultView(DISPLAY))
    undo_display = copy.deepcopy(config.getColorSpace(display_space).getTransform(ocio.COLORSPACE_DIR_FROM_REFERENCE))
    undo_display.setDirection(ocio.TRANSFORM_DIR_INVERSE)
    added_views = []
    for key, name in RENDERINGS:
        loocs_display = loocs.getDisplayViewColorSpaceName('sRGB', name)
        gamut = loocs.getColorSpace(loocs_display).getTransform(ocio.COLORSPACE_DIR_FROM_REFERENCE)
        view_name = rendering_view(key)
        transform = loocs.getViewTransform(loocs.getDisplayViewTransformName('sRGB', name))
        view = ocio.ViewTransform(ocio.REFERENCE_SPACE_SCENE)
        view.setName(view_name)
        view.setFamily('LOOCS')
        view.setDescription(f'LOOCS 0.12 {name} colour rendering, SDR.')
        view.setTransform(ocio.GroupTransform([
            copy.deepcopy(reference_to_709), copy.deepcopy(from_709),
            _renamed(transform.getTransform(ocio.VIEWTRANSFORM_DIR_FROM_REFERENCE), files),
            _renamed(gamut, files), copy.deepcopy(undo_display)]), ocio.VIEWTRANSFORM_DIR_FROM_REFERENCE)
        config.addViewTransform(view)
        config.addDisplayView(DISPLAY, view_name, view_name, display_space, '')
        added_views.append(view_name)

    for key, label, candidates in LOGS:
        space = next((c for c in candidates if config.getColorSpace(c)), None)
        if space is None:
            loocs_name = next((c for c in candidates if loocs.getColorSpace(c)), None)
            if loocs_name is None:
                continue
            scene_space(loocs_name, loocs_name, 'LOOCS/Log', 'log', f'LOOCS 0.12 {loocs_name}')
            space = loocs_name
        config.addDisplayView(DISPLAY, log_view(key), space, '')
        added_views.append(log_view(key))

    for key in CORRECTIONS:
        name = correction_space(key)
        scene_space(name, name, 'LOOCS/Texture correction', 'sdr-video',
                    f'sRGB texture with LOOCS 0.12 texture correction for {RENDERING_LABELS[key]}')

    views = list(config.getActiveViews())
    if views:
        config.setActiveViews(','.join(views + [v for v in added_views if v not in views]))
    config.addSearchPath(str(lut_path))
    description = config.getDescription()
    for marker in (LEGACY_MARKER, PREVIOUS_MARKER, MARKER):
        description = description.split('\n' + marker)[0]
    config.setDescription(description + '\n' + MARKER)
    return files


def _original_config(source, destination):
    """Blender's original config for the profile, never a Sparkle-extended one."""
    import PyOpenColorIO as ocio
    backup = destination / BACKUP
    config = ocio.Config.CreateFromFile(str(source))
    description = config.getDescription()
    if any(marker in description for marker in (MARKER, PREVIOUS_MARKER, LEGACY_MARKER)):
        if not backup.is_file():
            raise ValueError('The Sparkle colour profile backup is missing')
        return ocio.Config.CreateFromFile(str(backup)), backup.parent, True
    return config, source.parent, False


def install_profile(source, destination):
    """Extend a copy of the original config; keep all original views and LUTs."""
    import PyOpenColorIO as ocio
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    config, base, from_backup = _original_config(source, destination)
    if not config.getColorSpace('Linear Rec.709') or DISPLAY not in config.getDisplays():
        raise ValueError('This color profile does not define Linear Rec.709 and an sRGB display')
    # Resolve original search paths before moving the profile. Never edit the
    # Blender installation or files belonging to another OCIO configuration.
    paths = list(config.getSearchPaths())
    config.clearSearchPaths()
    config.addSearchPath(str(base))
    for path in paths:
        config.addSearchPath(str((base / path).resolve()))
    original_paths = list(config.getSearchPaths())
    original = config.serialize()
    # Build and validate beside the destination. A failed first setup must not
    # leave an empty colormanagement folder that overrides Blender's fallback.
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.parent / ('.sparkle-color-' + uuid.uuid4().hex)
    (staged / LUT_DIR).mkdir(parents=True)
    try:
        files = build_profile(config, staged / LUT_DIR)
        for name in sorted(files):
            write_cube(name, staged / LUT_DIR / ('loocs_' + name))
        config.validate()
        processor = config.getProcessor(ocio.ROLE_SCENE_LINEAR, DISPLAY, VIEW, ocio.TRANSFORM_DIR_FORWARD)
        processor.getDefaultCPUProcessor().applyRGB([.18, .18, .18])
        config.clearSearchPaths()
        for path in original_paths + [str(destination / LUT_DIR)]:
            config.addSearchPath(path)
        (staged / 'config.ocio').write_text(config.serialize(), encoding='utf-8')
        if not from_backup:
            (staged / BACKUP).write_text(original, encoding='utf-8')
        destination.mkdir(parents=True, exist_ok=True)
        if (destination / LUT_DIR).exists():
            shutil.rmtree(destination / LUT_DIR)
        os.replace(staged / LUT_DIR, destination / LUT_DIR)
        if (staged / BACKUP).exists() and not (destination / BACKUP).exists():
            os.replace(staged / BACKUP, destination / BACKUP)
        os.replace(staged / 'config.ocio', destination / 'config.ocio')
        # The 0.11 profile's single baked view is no longer referenced.
        (destination / 'sparkle_chromaluma.clf').unlink(missing_ok=True)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    return destination / 'config.ocio'


def setup_user_profile():
    source = Path(os.environ.get('OCIO', ''))
    if not source.is_file():
        raise ValueError('Cannot locate Blender\'s current color profile')
    destination = Path(bpy.utils.user_resource('DATAFILES')) / 'colormanagement'
    # An explicit environment override wins over Blender's user data directory.
    # Do not claim setup succeeded when a restart would ignore the new profile.
    known = [(Path(bpy.utils.resource_path(kind)) / 'datafiles/colormanagement/config.ocio').resolve()
             for kind in ('USER', 'LOCAL', 'SYSTEM')]
    known.append((destination / 'config.ocio').resolve())
    if os.environ.get('BLENDER_OCIO') or source.resolve() not in known:
        raise ValueError('A custom OCIO environment override is active. Remove that override and restart Blender before running this setup')
    if (destination / 'config.ocio').is_file():source = destination / 'config.ocio'
    return install_profile(source, destination)


class MCBEDROCK_OT_sky_color_profile(SkyOperator, bpy.types.Operator):
    bl_idname = 'mcbedrock.sky_color_profile'
    bl_label = 'Set up LOOCS colors'
    bl_description = ('Add the LOOCS 0.12 color renderings, log views and texture-correction color spaces to '
                      'Blender color management; save your project and restart Blender afterward')

    def execute(self, context):
        try:
            setup_user_profile()
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for scene in bpy.data.scenes:
            scene.mcbedrock_sky.native_color_pending = True
        self.report({'INFO'}, 'LOOCS colors installed. Save your project and restart Blender')
        return {'FINISHED'}


DATA_NODES = {'NORMAL_MAP', 'BUMP', 'DISPLACEMENT', 'VECTOR_DISPLACEMENT', 'SEPARATE_COLOR', 'SEPRGB', 'SEPARATE_XYZ'}
DATA_SOCKETS = ('normal', 'roughness', 'metallic', 'specular', 'height', 'ior', 'anisotropic', 'coat', 'weight')


def _feeds_color(socket):
    """The image's colour reaches shading as colour, not as packed data."""
    targets = []
    for link in socket.links:
        node = link.to_node
        if node.type == 'REROUTE':
            targets += [l for l in node.outputs[0].links]
        else:
            targets.append(link)
    if not targets:
        return False
    for link in targets:
        if link.to_node.type in DATA_NODES:
            return False
        if any(word in link.to_socket.name.lower() for word in DATA_SOCKETS):
            return False
    return True


def _color_images(material, seen=None):
    """8-bit colour image textures in a material, including nested groups."""
    seen = set() if seen is None else seen
    images = []

    def visit(tree):
        if tree is None or tree.as_pointer() in seen:
            return
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image and not node.image.is_float and _feeds_color(node.outputs['Color']):
                images.append(node.image)
            elif node.type == 'GROUP':
                visit(node.node_tree)
    visit(getattr(material, 'node_tree', None))
    return images


class MCBEDROCK_OT_texture_correction(bpy.types.Operator):
    bl_idname = 'mcbedrock.texture_correction'
    bl_label = 'Texture correction'
    bl_description = ('Set the selected objects\' sRGB color textures to the LOOCS texture-correction color space '
                      'for the current rendering, or back to sRGB')
    bl_options = {'REGISTER', 'UNDO'}
    enable: bpy.props.BoolProperty(default=True)

    def execute(self, context):
        scene = context.scene
        key = correction(scene)
        if self.enable and key == CLASSIC:
            self.report({'INFO'}, "Sparkle classic is applied by Sky & Lighting's materials; textures stay sRGB")
            return {'CANCELLED'}
        target = correction_space(key) if self.enable and key and key != CLASSIC else 'sRGB'
        if self.enable and not key:
            self.report({'WARNING'}, 'This rendering has no texture correction')
            return {'CANCELLED'}
        try:
            import PyOpenColorIO as ocio
            known = ocio.GetCurrentConfig().getColorSpace(target) is not None
        except Exception:
            known = False
        if not known:
            self.report({'ERROR'}, 'Set up LOOCS colors and restart Blender first')
            return {'CANCELLED'}
        spaces = {correction_space(k) for k in CORRECTIONS} | {'sRGB'}
        changed = skipped = 0
        materials = {slot.material for obj in context.selected_objects for slot in obj.material_slots if slot.material}
        for material in materials:
            # Sky & Lighting already corrects the copies it owns.
            if material.node_tree and material.node_tree.nodes.get('MC compensate Base Color'):
                skipped += 1
                continue
            for image in _color_images(material):
                if image.colorspace_settings.name in spaces and image.colorspace_settings.name != target:
                    image.colorspace_settings.name = target
                    changed += 1
        message = f'{changed} textures set to {target}'
        if skipped:
            message += f'; {skipped} Sky & Lighting materials already corrected'
        self.report({'INFO'}, message)
        return {'FINISHED'}


CLASSES = (MCBEDROCK_OT_sky_color_profile, MCBEDROCK_OT_texture_correction)
