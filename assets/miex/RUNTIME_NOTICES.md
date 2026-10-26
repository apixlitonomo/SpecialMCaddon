# Bundled World Exporter runtimes

The Windows x64 add-on includes the upstream binaries listed below. Working
files and generated Minecraft assets go into Blender's user-data directory,
not this installation. Minecraft game assets are read from the user's own
installed game versions and resource packs; they are not bundled here.

## MiEx

- Release: v2.0.0.experimental-28
- Binary: https://github.com/BramStoutProductions/MiEx/releases/download/v2.0.0.experimental-28/MiEx.jar
- SHA-256: 30f7779c2216791863585e6251613ebbb5bf1f933eb095df355d87e80b67c406
- Source: https://github.com/BramStoutProductions/MiEx/tree/e5f5ecaa885c52d2e1a095fed132f986c3d2e253
- License: BSD-3-Clause; see exporters/miex/LICENSE-MiEx.txt.

The original jar includes its upstream dependencies. The Sparkle adapter adds
commands through MiEx's command registry; it does not modify MiEx.jar. Adapter
source is in exporters/miex/MCAddonMiEx.java. Compile with Java 17 bytecode against
the pinned MiEx.jar, then place the resulting classes in exporters/miex/bridge.jar.

## Java

- Runtime: Eclipse Temurin OpenJDK 21.0.12.1+1, Windows x64 JRE, HotSpot
- Release: https://github.com/adoptium/temurin21-binaries/releases/tag/jdk-21.0.12.1%2B1
- Build input: OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip
- Build input SHA-256: f9d6e191ab098c0d416e7d588a24420a8621cd2f4720dab2459b8b7b2d2d8b4e
- Corresponding source: https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/OpenJDK21U-sources_21.0.12.1_1.tar.gz
- Original notices and licenses: jre/NOTICE and jre/legal (retained in full).

This is a custom runtime built with the pinned JDK's `jlink`, using
`tools/build_miex_runtime.py` in the project. `jre/mcaddon-runtime.json` records the
input archive, checksum and full build options. The runtime includes the modules
reported by `jdeps` for MiEx.jar and bridge.jar: java.base, java.compiler,
java.desktop, java.sql and jdk.unsupported, plus their transitive dependencies.
The dynamically loaded jdk.crypto.ec and jdk.zipfs providers are also retained.
Image/font support remains available for map previews and texture processing.
Unneeded Java modules, extended locales/charsets, development/diagnostic tools,
native linker inputs, debug metadata and optional CDS startup caches are omitted.
The module image uses jlink's ZIP level 9 compression. The reduced runtime is
tested with offline discovery, Java and Bedrock maps/exports, and resource packs.

References: https://docs.oracle.com/en/java/javase/21/docs/specs/man/jlink.html
and https://docs.oracle.com/en/java/javase/21/vm/class-data-sharing.html

The included Java runtime is used only for the add-on's MiEx process. Other
platforms can use an external Java executable through Advanced Setup; the bundled
runtime and this release's full integration tests target Windows x64.
