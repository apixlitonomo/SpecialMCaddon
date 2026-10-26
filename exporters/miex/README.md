# World Exporter

Requires Blender **4.5 LTS or newer**. Install the Sparkle release ZIP, then open **World Importer** in Blender's top
workspace row beside Layout and Modeling. The add-on creates this tab automatically
without switching away from your current workspace. There is no World Exporter
category in the N sidebar and no exporter page in the styled add-on interface.

The workspace uses the Sparkle charcoal surfaces, blue controls and Inter font.
**Choose world…** on the start card opens
the centered world library; Escape, the close button or an outside click dismisses it.
Once loaded, a top-down map fills the workspace. The top bar above it shows the
open world (click it to switch worlds) and tabs for the world's dimensions.
After terrain is ready, the sidebar eases in over 200 ms at the right edge:
Resource packs at the top, then Area, Look, Save and More options. Sections
open and close independently.
The N key or map context menu reopens it if closed.
Import stays pinned at the bottom of the sidebar while its settings scroll. There are no alternate 3D views. Player dots and UUID labels are not drawn on the map.
The empty canvas is plain charcoal; chunk
lines appear only while selecting/excluding chunks or editing foreground groups.
Timeline, Outliner and unrelated panels are removed only from this workspace.
Existing exporter workspaces upgrade in place, preserving their names.

1. Click **Choose world…** (or the world name in the top bar), then choose a world from the searchable library of installed Minecraft launchers.
   Cards use the Resource Pack Manager's fonts, search styling and edition tags:
   green for Java and blue for Bedrock, with the version beside the tag and
   launcher underneath. Java thumbnails use `icon.png`;
   Bedrock uses `world_icon.jpeg` when available. Missing metadata keeps a world
   selectable with a neutral icon and “Version unavailable”. Search includes
   world names, versions, editions and launcher instances. All worlds appear
   in one searchable list with launcher labels and no edition dividers. Hytale
   is excluded. The blue card marks the loaded world; arrow-key focus uses an
   outline. Opening or refreshing reveals cards in a fast sequence (disabled by
   Reduce interface motion). **Refresh worlds** rescans; **Browse folder** opens a custom save.
2. Browse its actual chunk imagery before importing. Middle or right mouse drag
   pans, the wheel zooms around the pointer, and left drag selects blocks. Snap to
   chunks selects whole chunks. Trackpad pan/zoom events are supported. Drag a
   corner or anywhere along an edge to resize, or drag inside the selection to move it. Esc cancels a
   gesture; Undo restores a selection edit. Drag numeric fields horizontally to
   adjust them (hold Shift for finer control); Esc or right-click cancels the
   drag. A click opens the native Blender editor for typing and clipboard use.
   Map interaction hints appear in Blender's native status bar.
3. Set the vertical bounds; drag to exclude chunks, edit foreground groups or add named
   regions. Auto origin preserves the relative placement of multiple regions.
4. Turn resource packs on with the switches under **Resource packs**. Search
   filters the cards; the folder icon adds a pack folder and the arrows rescan.
   Enabled packs list first, in priority order: drag a card to change its
   priority, or right-click it for Move up, Move down, Move to top and Remove.
   The first pack has highest priority; changing it updates the same shared
   stack everywhere, including the RP Manager.
   Loading a preset restores the shared pack order; if a source is missing,
   the current stack stays intact and the map shows which packs need attention.
   MiEx includes the Blender
   template, world dependencies (when enabled) and vanilla base assets.
5. Select Cycles or EEVEE materials under **Look** and choose **Import**.
   Turn off **Import into Blender when done** under **Save** to export USD only.

Geometry controls include optimisers, cave removal/fill, individual blocks and
LOD. Entity and animation settings are under Advanced settings. Selection presets
can be saved and restored; existing MiEx USD metadata can be read without importing
geometry. Tools expose atlas creation, PBR generation and modpack extraction.
MiEx performs these operations in a background process. Map textures stay outside
Blender scene datablocks and are cached with a bounded visible chunk count.
Invalid bounds are explained beside the export action. Settings are locked during
explicit jobs, while map navigation and inspector browsing remain available.
New worlds start at X=0, Z=0, with a closer view spanning at most 256 blocks
along the longer viewport axis (within the supported zoom range). Saves without
terrain at the origin still allow panning and Go to player. Pack refreshes keep
the current map position and zoom. Go to player and Fit world are in the map
context menu; the wheel zooms. Selection dimensions appear as plain white text
above the selected area.

**Flat | 3D** (the switch at the map's top-left) swaps the flat map for the
selected area in 3D, like Mine-imator's world import: the map tilts into a solid
cut-out of the area, loading from the middle out. Drag to orbit, middle-drag or
Shift-drag to pan, scroll to zoom, Home to reset, right-click for more. It follows
Min and Max edits and stays at full block detail up to about 256 x 256 blocks;
larger areas (up to 2,048 x 2,048) keep the middle at full detail. It adds
nothing to the scene. Every world opens on Flat; click Flat to go back.

MiEx's terrain-height shading runs on the GPU. Large texture pages combine up to
256 chunks per draw and include neighbouring heights to avoid page seams. CPU
page preparation is budgeted outside drawing, with vectorized color conversion
and a small upload queue. Old textures remain visible while dirty pages refresh.
Drawing computes page bounds directly; chunk request ordering is cached separately.
The GPU texture cache is capped at 324 pages (about 205 MiB of texture payload);
compact signed height arrays also reduce CPU tile storage.

The scanner keeps MiEx's native launcher discovery and adds standard Prism,
PolyMC, CurseForge, ATLauncher, FTB and GDLauncher locations. Custom MiEx launcher
environment paths remain supported. When a world is newer than the vanilla base pack,
the bridge reads the world version inside every installed client jar (all launchers,
plus CurseForge's shared `Install/versions`) and uses the newest; mod-loader profiles
without a jar are skipped. Only if none is new enough does it download the official
client from Mojang's version manifest (latest release, then latest snapshot). If that
fails too, the world still opens with a warning that its newest blocks may look wrong.
Tools also includes **Update vanilla base pack**. No Minecraft game assets ship in the release ZIP.
Native biome water tints have a fallback below selected/world packs, because
liquids lack model tint indices for the base-pack generator to discover. Biome
colors and pack overrides work in the preview and use the same tint rules for export.
Pack preparation only runs when world/dimension/pack settings change, not on each
map tile request.

The result is added to a new collection in the current scene. Existing objects and
materials are preserved. Changing the active scene while exporting leaves the
export ready for manual import in its destination scene. Opening another blend
cancels the old job. Imports can be undone; failed imports retain their export
for **Import Last Export**.

Importing includes the official MiEx Blender importer's material reconstruction,
render variant, 1/16 scale, flat shading and viewport colour correction. Its
material code is bundled and adapted for current Blender transparency APIs;
the separate MiEx Import add-on does not need to be installed. We additionally
isolate new materials and roll back a failed import to protect the existing scene.

The Windows x64 ZIP includes MiEx and Java: no separate app/runtime install is
needed. Leave Advanced settings overrides blank to use these files. MiEx runs as a
hidden process and stores working files in Blender's user-data folder. Minecraft
assets come from the user's installed game and packs, not bundled game assets.
The first start can take longer while MiEx prepares those assets.

Exports default to `MiEx Exports` beside a saved blend, or persistent user storage
for unsaved files. Material textures are copied beside each export. Keep that
directory when moving or sharing a blend; saving the blend does not pack images.

Windows x64 is the bundled runtime target. Other platforms require an
external Java installation through Advanced settings. See RUNTIME_NOTICES.md in
assets/miex for pinned versions, licensing, and source links.

Particle-only vanilla water/lava blockstates are removed from generated session pack copies so MiEx uses its built-in fluid geometry. Source packs and custom models are retained.

The world controls stay centered over the map as the dock changes width. Right-click opens
map, selection or pack actions; right-drag pans without opening a menu. Menus
support arrow keys, Enter and Escape; Tab moves between visible controls.
Settings → Tools includes Reduce motion. Final Blender import work shows a
non-cancellable finishing state; failed imports keep their export for retry.
Switching workspaces retains the map cache, zoom and selection;
the hidden map pauses interaction and resumes without reloading cached chunks.

MiEx warms up and discovers worlds in the background on startup when the
automatic World Exporter workspace is enabled. The world picker stays usable;
terrain is only loaded after choosing a world. Selection handles and the panel
reveal wait for terrain or a completed empty preview. Search supports click-to-focus,
a blinking caret, type-ahead, Ctrl+A and paste. Refreshed world cards reveal in
120 ms with a 12 ms stagger; Reduce motion disables this animation.

The bundled MiEx build is `v2.0.0.experimental-28`. It includes native Minecraft
26.3 default block properties, fixes for exported geometry and region bounds, and
the `chunkPreview` command behind the map's 3D preview.
`MCAddonBlockDefaults` remains available for cached vanilla base packs created
before this upstream support. It supplies the official defaults before MiEx
decodes modern chunk palettes. Explicitly saved properties
and legacy `Name`/`Properties` entries are preserved. Source worlds are not
rewritten. The checked-in `block_defaults_26_3.json` contains property metadata
generated by the installed Minecraft 26.3 `net.minecraft.data.Main --reports`.
Rebuild with `python tools/build_miex_bridge.py --jdk <JDK folder>`; optionally pass
`--block-report <reports/blocks.json>` to regenerate the metadata for this version.
The upstream MiEx jar is bundled without modification. Regression coverage includes compact
strings, heterogeneous-list wrappers, explicit snowy/horizontal states, legacy
states, exported tint networks and imported Blender color attributes.
