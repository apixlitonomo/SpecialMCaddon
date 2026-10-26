# Vibrant Visuals water resources

These water textures were recovered read-only from the user's installed
Minecraft for Windows **1.26.5203.0** on **2026-10-04**, after the user expressly
authorized taking and adapting Vibrant Visuals shaders and textures. This
records the user's stated permission; it does not relicense Mojang's artwork
under Sparkle's code license.

The source is the installed pack archive
`data/resource_packs/vanilla_1.21.90/__brarchive/textures/blocks.brarchive`.
Later installed vanilla packs did not override these water textures. Sparkle's
validated BRArchive reader extracted only water entries. Color PNGs are copied
byte-for-byte. Indexed-color normal TGAs are expanded to RGB PNGs and MERS TGAs
to RGBA PNGs without resampling, recoloring, channel inversion or pixel edits.
Decoded RGBA pixel equality was verified against every source image.

| Packaged file | Source filename | Size | Animation frames |
|---|---|---:|---:|
| `water_still_grey.png` | same | 16 × 512 | 32 |
| `water_flow_grey.png` | same | 32 × 1024 | 32 |
| `water_still_grey_normal.png` | `water_still_grey_normal.tga` | 16 × 512 | 32 |
| `water_flow_grey_normal.png` | `water_flow_grey_normal.tga` | 32 × 1024 | 32 |
| `water_still_grey_mers.png` | `water_still_grey_mers.tga` | 16 × 512 | 32 |
| `water_flow_grey_mers.png` | `water_flow_grey_mers.tga` | 16 × 512 | 32 |

Each atlas contains vertically stacked square frames, starting at the top.
Sparkle's Vibrant water material samples the four normal and MERS atlases.
The two grey color atlases are retained as source references; they are not
shader inputs. Blender supplies the water tint and optical shading.
The installed vanilla `textures/flipbook_textures.json` specifies 2 ticks per
still-water frame (10 frames per second), and `replicate: 2` for flowing water.
It does not explicitly specify flowing-water ticks per frame.

The corresponding texture-set definitions bind the grey color, normal and MERS
files. Normal and MERS are linear data, not display colors. MERS channels are
metalness, emissive, roughness and subsurface, respectively. In these images,
red is 18/255 and green/alpha are zero. Still-water roughness ranges from
15/255 to 87/255, with a mean of 21.73/255; flowing-water roughness ranges from
8/255 to 30/255. Blender's optical shading remains an adaptation: a MERS channel
does not imply that Blender's Principled parameters have identical semantics
inside Bedrock's special water rendering path.

The installed `vanilla_1.21.120` default water settings have procedural waves
**disabled** and all three particle concentrations set to zero. The authored
normal-map animation is therefore the appropriate default surface detail.
The default caustics animation is enabled at 0.09 seconds per frame, scale 0.5
and power 1. `caustics.png` is the original, byte-identical renderer texture from
`data/renderer/caustics.png`: 32 × 2048 pixels, with 64 vertically stacked frames.
Sparkle projects this animation onto owned overlays on the first opaque,
upward-facing submerged receiver. Coverage, depth and daylight restrict the
effect. It uses nearest sampling at two-block tiles, matching 1/16-block texels.
This is an authored-texture projection, not Bedrock's deferred caustics pass;
it does not trace refracted photon paths. Receiver modifiers are evaluated when
applying water; coverage is baked, so reapply after moving or editing geometry.
Cycles uses additive light. EEVEE uses a bright dithered emission overlay because
its screen-traced water cannot show additive transparency. Projection has a
20-second work budget, processes higher water first, and reports partial
coverage when a large export reaches that limit.

The installed renderer contains compiled `DeferredWater`, `WaterPrepass`,
`WaterExtinction` and `WaterSimulation` material binaries. Local Direct3D shader
disassembly confirmed normal decoding from [0,1] to [-1,1] and transformation
through tangent, bitangent and normal vectors. Original HLSL/GLSL source was not
present. Compiled shaders, archive containers and disassembly are development
references only and are not packaged. This is an adaptation using original
water textures, not an executable port of Bedrock's complete renderer.

The inspected `DeferredWater` shader uses a water-specific Schlick Fresnel term
`0.02 + 0.98 * (1 - dot)^5` and GGX shading, with direct-light roughness clamped
to at least 0.05. Consequently the MERS red channel should not be copied into
Blender's metallic input: this is a dielectric water surface with approximately
2% normal-incidence reflectance. The inspected `WaterExtinction` shader computes
`exp(-depth * extinction)` from scene depth and runtime RGB extinction
coefficients. Reusing these normal and roughness textures does not reproduce
that depth pass or the game's screen-space reflections.

SHA-256 of packaged PNG bytes:

```text
db3edc3132d88a595d466e90840a8e8839593776421435c23aa880fbd2a779c3  water_still_grey.png
f803fa03de42513b8e19db50e8c0743c5c82a06337afe0191efddd3e47684246  water_flow_grey.png
c21455997741ed07cde8617fb7ced4658b164df6dcc1214f2ef40a62a50ff2b4  water_still_grey_normal.png
4f370d329b5c4c22c24c545990171b74f7be2ce740d0a182785a1e8fd3c086fe  water_flow_grey_normal.png
f14d9181802046a3cee4183800fd42c5d1a2454e81c521f8fea9ed037cbf3b11  water_still_grey_mers.png
9d783caca4919f120a428fe35687a7d4c4dfe5468139b9bb8bb2676458ff9a85  water_flow_grey_mers.png
011397d8647ae3a434aaf2d6a1c6b2f67daa319cdb04e7da65eb3e12e449628d  caustics.png
```

Primary documentation:

- [Microsoft: Texture Sets](https://learn.microsoft.com/en-us/minecraft/creator/reference/content/texturesetsreference/texturesetsconcepts/texturesetsintroduction?view=minecraft-bedrock-stable)
- [Microsoft: Animated Block Textures](https://learn.microsoft.com/en-us/minecraft/creator/documents/createanimatedblocktexture?view=minecraft-bedrock-stable)
- [Microsoft: Water Effects](https://learn.microsoft.com/en-us/minecraft/creator/documents/vibrantvisuals/watercustomization?view=minecraft-bedrock-stable)

Detailed local extraction records are under `test_output/vibrant_reference` in
the development workspace and are not included in the release.
