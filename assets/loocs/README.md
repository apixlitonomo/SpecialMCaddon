# LOOCS colour integration

Source: the user's `LOOCS V0.12.zip`, supplied by Bram Stout. Copyright 2024
Bram Stout Productions; see LICENSE.txt for redistribution terms.

## Where it lives in Blender

Render Properties > Color Management has an inline **LOOCS** section, right
under View and Look:

- **Set Up LOOCS Colors** (or **Update to LOOCS 0.12** after the 0.11 setup)
  installs a Blender user colour profile. Save the project and restart once.
- The header checkbox shows the scene through LOOCS. **Rendering** picks one of
  Bram's ten SDR colour renderings; Chroma Luma is the default.
- **Output** switches to a log view (LOOCS Log, ARRI LogC3/LogC4, S-Log3,
  S-Log3 Cine, V-Log, RED Log3G10, REDLogFilm) for grading with the matching
  LOOCS input DCTL.
- **Textures** applies Bram's texture correction (below).

Every rendering and log view also appears in Blender's own View menu, so the
choice is ordinary colour management: it works in Material Preview, Rendered
view and final renders with compositing off, and saves with the file. The
compositor outputs scene-linear colour, so LOOCS is never applied twice.

## What setup writes

Setup extends a copy of Blender's current config in the user's
`datafiles/colormanagement` directory. It never edits the Blender
installation. It keeps every original colour space, display, view and LUT
search path, and saves `sparkle-original-config.ocio` beside it. To undo,
replace `config.ocio` with that backup and restart. Running setup again always
rebuilds from the backup, so it is repeatable. An external `OCIO` /
`BLENDER_OCIO` override must be removed first, since it wins at startup.

On the sRGB display it adds:

- views `LOOCS <Rendering>` for Filmic, Filmic Classic, Filmic Classic BW,
  Filmic Vintage, Neutral, Animation, Flat, Chroma Luma, HVS and Painter;
- views `Log · <Encoding>`. Blender's own camera log spaces are used where they
  exist; LOOCS Log and REDLogFilm are imported from LOOCS;
- input colour spaces `sRGB TC <Rendering>` (texture correction).

Each view carries Bram's display gamut mapping (G1/G2) itself and shows on the
display's own sRGB colour space, undoing its encoding at the end. Blender's GPU
viewport skips a view's own display colour space: with the mapping in separate
`LOOCS sRGB - Display G1/G2` spaces (profile v2), F12 renders were right but
Rendered view showed a pure, electric blue daylight sky. CPU output is
identical either way. Scenes on a v2 profile get **Update LOOCS Colors** in the
LOOCS section; restart once afterward.

Transforms are read from `loocs_config.ocio` (Bram's reduced config, unchanged)
and wrapped between Blender's reference space and LOOCS G1. LUT file names get
a `loocs_` prefix inside `colormanagement/sparkle_loocs` (about 86 MB). Against
Bram's own config, renderings match within 0.0006 (float16 storage), texture
correction exactly, and log views within 0.003 (Blender's camera log
definitions).

## Texture correction

LOOCS 0.12 ships one 9³ LUT per rendering for sRGB textures made without a
colour rendering in mind. It maps the stored sRGB value to a corrected sRGB
value; LOOCS then decodes with a 2.2 gamma. Filmic Classic BW uses Filmic
Classic's correction; Painter has none.

With **Auto-Correct** on (default), Sparkle sets material colour textures that
are plain sRGB to the `sRGB TC` space of the current rendering, including MiEx
worlds and model imports as they arrive, and follows later rendering changes.
It tags the textures it switched and returns them to sRGB when LOOCS is off.
Textures feeding normals, bumps, channel splits or roughness-style sockets,
textures the user set to another colour space, and the sky's own artwork are
left alone. **Correction** can override the match (mix and match is allowed).

Chroma Luma's match is **Sparkle classic** instead: Sparkle's own inverse of
the Chroma Luma view (`inverse_srgb.npy`, 33³, linear Rec.709 in and out,
indexed with the sRGB encoding), applied by shader nodes to the final surface
colour of Sky & Lighting materials and to a quarter of the daylight sky, with
textures left sRGB. It is the look Sparkle had before 0.12; Bram's Chroma Luma
correction made MiEx worlds about a fifth darker and is still a named choice.

With Auto-Correct off, Sky & Lighting materials get the same LUT as shader
nodes (`lighting_pipeline.compensation`, scaled by Texture compensation), and
**Correct Selected / Restore sRGB** set colour spaces by hand.

## Files

- `loocs_config.ocio`: Bram's `config_small.ocio`, the transform source.
- `renderings.npz`: the ten SDR rendering LUTs and the sRGB G1/G2 display LUTs,
  65³, float16 in `.cube` row order (red fastest).
- `texture_correction.npz`: the eight sRGB texture-correction LUTs, 9³, float32.
- `inverse_srgb.npy`: Sparkle classic, the 33³ inverse of Chroma Luma (Sparkle's own).
- `matrices.json`: matrices for `builders/loocs_transform.py` (the 0.11
  reference transform kept for its tests).
- `blue_noise.npy`: a deterministic 64² periodic void-and-cluster rank texture,
  generated by Sparkle, used as an optional spatial opacity threshold in UV
  space. Built-in transparency remains the default.

Rebuild the LUT archives from an extracted LOOCS release:
`python tools/build_loocs_assets_v012.py "<LOOCS V0.12 folder>"`.
Rebuild the blue noise with `python tools/build_loocs_assets.py` (NumPy, SciPy).
Nothing is downloaded when using the add-on.
