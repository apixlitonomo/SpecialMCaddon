# QUICK INSTRUCTIONS
1. Just drag any .jar minecraft mod to blender and wait
2. profit
3. support Sparkle devs ( https://discord.com/invite/UXqXvCvc5C )

# Sparkle

**A Minecraft Blender suite** — import models, particles, schematics, and full worlds; manage resource packs; simulate Bedrock/Java particles with Molang; and export back to Blockbench and Bedrock formats.

**Addon name (in Blender):** Sparkle  
**Version:** 1.3.1  
**Minimum Blender:** 5.2.0  
**Authors:** MrRobot and TylerBits  
**License:** GPL-2.0-or-later  
**Discord:** [Sparkle community](https://discord.com/invite/UXqXvCvc5C)

---

## Overview

Sparkle is a comprehensive Blender add-on for Minecraft content creators. It bridges Minecraft assets (Bedrock and Java) and Blender so you can:

- Drag-and-drop **Minecraft mod `.jar` files** and import mobs, blocks, and items
- Browse and import assets from **resource packs** (Bedrock and Java)
- Import and simulate **particles** (Bedrock JSON + Molang, Java particles)
- Import **schematics** (Litematica, Axiom, Sponge/WorldEdit, legacy MCEdit)
- Export/import **Minecraft worlds** via a built-in World Exporter (MiEx-based)
- Work with **Blockbench projects** (`.bbmodel`) with round-trip animation export
- Style scenes with **Sky & Lighting**, outlines, posterize, and film-look tools

Primary UI: **View3D → Sidebar → Sparkle** (custom GPU-styled interface, with optional native Blender UI).

---

## Features

### 1. Direct Minecraft Mod (`.jar`) Import

- **Drag any `.jar` mod onto the 3D viewport** (or use **File → Import → Minecraft Mod Model (.jar)**).
- A floating panel lists importable **mobs / entities**, **models**, **blocks**, and **items**.
- Supports:
  - Entity renderers and Citadel-style models
  - Vanilla-style `LayerDefinition` factories
  - GeckoLib geo models and animations
  - Java JSON block/item models
- Texture variants are ranked and selectable; models are built at the 3D cursor.
- The JAR is cached so moving/deleting the original file does not break the session.

### 2. Resource Pack Manager

- Discover installed Bedrock and Java resource packs.
- Add manual packs, favorites, and exclusions.
- Active pack stack behaves like the game (later packs override earlier ones).
- Pack icons, metadata, and background scanning with caching.

### 3. Asset Browser

- Index models, entities, particles, textures, and animations from active packs.
- List/grid views, filters (All / Importable / Models / Entities / Particles / Textures).
- Thumbnails generated in the background.
- One-click import of selected assets into the scene.

### 4. Particle System

- Import **Bedrock particle JSON** and **Java particle** definitions.
- Full **Molang** support (parser, compiler, runtime, metadata).
- Simulation backends (including Geometry Nodes cache).
- Import controls:
  - Emission: Authored / Burst / Continuous
  - Vertical motion: Authored / Rise / Fall / Custom
  - Lifetime and collision overrides
  - Simulation seed for reproducible previews
- Support for `.brarchive` particle archives.
- Controllable emitters with metadata for re-bake and preview.

### 5. Schematics

- Dependency-free NBT reader (no external libraries required).
- Formats: **Litematica**, **Axiom**, **Sponge/WorldEdit**, legacy **MCEdit**.
- Builds mesh geometry from block data using the active resource packs.
- Chunk-based spatial handling for large structures.

### 6. World Exporter / Importer

- Integrated **MiEx**-based workflow for Minecraft worlds.
- Map preview, selection, pack handling, and Blender material templates.
- Bundled runtime components (see third-party notices).

### 7. Blockbench & Model Pipeline

- Import `.bbmodel` projects and Minecraft JSON models/entities.
- Export animations back to `.bbmodel` while preserving original geometry, textures, and project metadata.
- Bedrock geometry/project export support.
- Materials, UVs, bones, and Minecraft-accurate transforms.

### 8. Scene Styling Tools

- **Sky & Lighting** — sky, sun/moon, clouds, fog, bounce lighting, sun rays, etc.
- **Outlines** — stroke/outline generation.
- **Posterize** — pixel-art / posterization workflows.
- Water, atmosphere, film-look, and related builders.

### 9. Updates & Preferences

- Network permission for update checks.
- Addon preferences (including option to use native Blender UI instead of the styled sidebar).

---

## Installation

1. Download the add-on as a **ZIP** (repository root or release package).
2. In Blender: **Edit → Preferences → Add-ons → Install from Disk** (or Install).
3. Select the ZIP and enable **Sparkle**.
4. Open the 3D Viewport sidebar (**N**) → **Sparkle** tab.

**Requirements**

- Blender **5.2.0** or newer.
- No separate Java install required for core features (a bundled JRE is used where needed for world tools).

---

## Quick Start

### Import a mod model

1. Drag a Minecraft mod `.jar` onto the 3D viewport **or** use **File → Import → Minecraft Mod Model (.jar)**.
2. Wait for the catalog panel to appear.
3. Search/select a mob, block, or item (and texture if available).
4. Confirm — the model is built at the 3D cursor.

### Browse resource packs

1. Open the **Sparkle** sidebar → **Resource Packs**.
2. Let the pack scan finish (or refresh).
3. Activate packs, then switch to **Assets** to browse and import models/entities/particles/textures.

### Import particles

1. **File → Import** particle JSON (or use the Particles page / Quick Import).
2. Optionally adjust emission, motion, lifetime, and seed.
3. Play the timeline to preview the simulated effect.

### Import a schematic

1. Use the **Schematics** page or the corresponding import operator.
2. Choose a supported schematic file.
3. The structure is decoded and meshed using your active packs.

---

## Project Structure (high level)

| Path | Role |
|------|------|
| `__init__.py` | Addon registration, operators, bl_info |
| `blender_manifest.toml` | Extension manifest (id, version, permissions) |
| `builder.py` | Particle system building & controls |
| `importers/` | JAR/mod, Bedrock/Java assets, BBModel, schematics, packs, thumbnails, VFS |
| `builders/` | Geometry, materials, sky, water, lighting, schematic mesh, mod models |
| `exporters/` | BBModel animation export, Bedrock project, MiEx world tools |
| `particle_core/` | Particle IR, runtime, events, UV, semantics |
| `ui/` | Sidebar app, asset browser, pack manager, schematic UI, world exporter UI, GPU framework |
| `molang*.py` | Molang parse / compile / runtime |
| `materials.py`, `parser.py`, `java_parser.py`, `simulator.py` | Materials, particle parsing, simulation |
| `assets/`, `data/` | Fonts, icons, sky textures, mappings, runtime assets |

---

## Capabilities Summary

| Capability | Details |
|------------|---------|
| Mod JAR import | Entities, Citadel models, LayerDefinitions, GeckoLib geo, Java block/item models |
| Resource packs | Discover, activate, stack, favorites, manual packs |
| Asset types | Models, entities, particles, textures, animations |
| Particles | Bedrock + Java, Molang, bake/preview, emission & motion overrides |
| Schematics | Litematica, Axiom, Sponge/WorldEdit, legacy MCEdit |
| Worlds | Import/export via MiEx integration |
| Blockbench | Import `.bbmodel`, export animations with loss-preserving metadata |
| Rendering aids | Sky, lighting, fog, water, outlines, posterize |

---

## Third-Party Components

See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for full licenses and attributions. Notable integrations include:

- **MiEx** (BSD-3-Clause) — world import and materials  
- **MCblend** ideas (MIT) — Bedrock importer structure  
- **WorldEdit** mappings (GPL-3.0-or-later) — legacy schematic block IDs  
- **Inter** font (SIL OFL 1.1)  
- **Phosphor Icons** (MIT)  
- **LOOCS** color management (Bram Stout Productions)  
- Bundled **Eclipse Temurin OpenJDK** JRE for world tooling (where applicable)

---

## Support & Community

- Discord: [https://discord.com/invite/UXqXvCvc5C](https://discord.com/invite/UXqXvCvc5C)
