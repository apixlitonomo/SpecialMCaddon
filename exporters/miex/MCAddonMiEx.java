// MCAddon compatibility commands for MiEx 2.0 experimental-28.
// Original MCAddon code. MiEx is loaded from the user's unmodified installation.
import com.google.gson.*;
import java.util.*;
import java.lang.reflect.Field;
import nl.bramstout.mcworldexporter.*;
import nl.bramstout.mcworldexporter.commands.*;
import nl.bramstout.mcworldexporter.export.ExportData;
import nl.bramstout.mcworldexporter.resourcepack.*;

public class MCAddonMiEx {
    public static void main(String[] args) {
        MCAddonBlockDefaults.install();
        // MiEx can keep worker threads alive after stdin closes. Do not leave
        // Java/JAR files locked when Blender exits or crashes on Windows.
        ProcessHandle.current().parent().ifPresentOrElse(
            parent -> parent.onExit().thenRun(() -> Runtime.getRuntime().halt(0)),
            () -> Runtime.getRuntime().halt(0));
        CommandRegistry.registerCommand("mcaddonConfigure", Configure.class);
        CommandRegistry.registerCommand("mcaddonPacks", Packs.class);
        CommandRegistry.registerCommand("mcaddonAtlas", Atlas.class);
        CommandRegistry.registerCommand("mcaddonPbr", Pbr.class);
        CommandRegistry.registerCommand("mcaddonMap", MapChunks.class);
        CommandRegistry.registerCommand("mcaddonDiscover", Discover.class);
        CommandRegistry.registerCommand("mcaddonBase", BasePack.class);
        CommandRegistry.registerCommand("mcaddonPreview", MCAddonPreview.class);
        MCWorldExporter.main(args);
        System.exit(0);
    }
    // Upstream picks the newest release date, which can be a mod-loader profile without a
    // client jar. Rank every installed client jar by the world version inside it instead,
    // download the official client only when none is new enough, and never refuse a world
    // because its assets are older than it.
    public static class BasePack extends Command {
        public JsonObject run(JsonObject cmd) {
            int required = cmd.has("worldVersion") ? cmd.get("worldVersion").getAsInt() : 0;
            int current = baseVersion();
            JsonObject result = new JsonObject();
            if (required == 0 || required > current) {
                java.io.File best = null; String label = null; int bestVersion = 0;
                if (nl.bramstout.mcworldexporter.launcher.LauncherRegistry.getLaunchers().isEmpty())
                    nl.bramstout.mcworldexporter.launcher.LauncherRegistry.initLaunchers();
                for (nl.bramstout.mcworldexporter.launcher.Launcher launcher :
                        new ArrayList<>(nl.bramstout.mcworldexporter.launcher.LauncherRegistry.getLaunchers())) {
                    // MiEx runs offline; the official download below replaces its online list.
                    if (launcher instanceof nl.bramstout.mcworldexporter.launcher.LauncherOnlineMC) continue;
                    for (nl.bramstout.mcworldexporter.launcher.MinecraftVersion version : versions(launcher)) {
                        java.io.File jar = localJar(version);
                        int found = jarVersion(jar);
                        if (found > bestVersion) { best = jar; bestVersion = found; label = launcher.getName() + "/" + version.getLabel(); }
                    }
                }
                // Launchers MiEx does not list versions for (CurseForge keeps clients in a shared Install folder).
                for (JsonElement element : cmd.has("clientFolders") ? cmd.getAsJsonArray("clientFolders") : new JsonArray()) {
                    java.io.File[] folders = new java.io.File(element.getAsString()).listFiles(java.io.File::isDirectory);
                    if (folders == null) continue;
                    for (java.io.File folder : folders) {
                        java.io.File jar = new java.io.File(folder, folder.getName() + ".jar");
                        int found = jar.isFile() ? jarVersion(jar) : 0;
                        if (found > bestVersion) { best = jar; bestVersion = found; label = folder.getName(); }
                    }
                }
                java.io.File download = null;
                // Only reach the network when nothing installed is new enough.
                if (bestVersion < Math.max(required, 1)) {
                    try {
                        download = java.io.File.createTempFile("mc_jar_file", ".jar");
                        String[] downloaded = downloadClient(required, download);
                        int found = downloaded == null ? 0 : jarVersion(download);
                        if (found > bestVersion) { best = download; bestVersion = found; label = "Mojang/" + downloaded[0]; }
                    } catch (Exception ex) { System.out.println("Could not download the Minecraft client: " + ex); }
                }
                try {
                    if (best != null && bestVersion > current) {
                        java.lang.reflect.Method update = ResourcePackDefaults.class.getDeclaredMethod(
                            "updateBaseResourcePack", java.io.File.class, String.class);
                        update.setAccessible(true);
                        update.invoke(null, best, label);
                        ResourcePacks.load();
                    }
                } catch (Exception ex) {
                    throw new RuntimeException("Could not prepare Minecraft assets from " + label + ": " + ex, ex);
                } finally { if (download != null) download.delete(); }
                current = baseVersion();
                if (current <= 0) throw new RuntimeException("Sparkle couldn't find Minecraft Java Edition on this computer or download it. Install Minecraft, or connect to the internet, then reload.");
                if (required > current) result.addProperty("warning",
                    "This world is newer than the Minecraft assets available, so its newest blocks may look wrong. Connect to the internet or launch the world's Minecraft version, then reload.");
            }
            result.addProperty("worldVersion", current);
            return result;
        }
        static int baseVersion() {
            try { return ResourcePacks.getBaseResourcePack().getWorldVersion(); } catch (Exception ex) { return 0; }
        }
        static List<nl.bramstout.mcworldexporter.launcher.MinecraftVersion> versions(nl.bramstout.mcworldexporter.launcher.Launcher launcher) {
            try {
                List<nl.bramstout.mcworldexporter.launcher.MinecraftVersion> versions = launcher.hasVersions() ? launcher.getVersions() : null;
                return versions == null ? Collections.emptyList() : versions;
            } catch (Exception ex) { return Collections.emptyList(); }
        }
        // The latest release covers every released world; a snapshot is used only when the release is still too old.
        static String[] downloadClient(int required, java.io.File target) throws Exception {
            JsonObject manifest = readJson("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json");
            JsonObject latest = manifest.getAsJsonObject("latest");
            for (String kind : new String[] {"release", "snapshot"}) {
                String id = latest.get(kind).getAsString();
                for (JsonElement element : manifest.getAsJsonArray("versions")) {
                    JsonObject row = element.getAsJsonObject();
                    if (!id.equals(row.get("id").getAsString())) continue;
                    String url = readJson(row.get("url").getAsString()).getAsJsonObject("downloads").getAsJsonObject("client").get("url").getAsString();
                    System.out.println("Downloading Minecraft " + id + " client from " + url);
                    try (java.io.InputStream in = new java.net.URI(url).toURL().openStream()) {
                        java.nio.file.Files.copy(in, target.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING);
                    }
                    if (required <= 0 || jarVersion(target) >= required) return new String[] {id};
                }
            }
            return target.length() > 0 ? new String[] {latest.get("snapshot").getAsString()} : null;
        }
        static JsonObject readJson(String url) throws Exception {
            java.net.URLConnection connection = new java.net.URI(url).toURL().openConnection();
            connection.setConnectTimeout(10000); connection.setReadTimeout(60000);
            try (java.io.Reader reader = new java.io.InputStreamReader(connection.getInputStream(), java.nio.charset.StandardCharsets.UTF_8)) {
                return JsonParser.parseReader(reader).getAsJsonObject();
            }
        }
        static java.io.File localJar(nl.bramstout.mcworldexporter.launcher.MinecraftVersion version) {
            try {
                java.net.URL url = version.getJarFile();
                if (url == null || !"file".equals(url.getProtocol())) return null;
                java.io.File jar = new java.io.File(url.toURI());
                return jar.isFile() ? jar : null;
            } catch (Exception ex) { return null; }
        }
        static int jarVersion(java.io.File jar) {
            if (jar == null) return 0;
            try {
                java.lang.reflect.Method read = ResourcePackDefaults.class.getDeclaredMethod("getWorldVersionFromJar", java.io.File.class);
                read.setAccessible(true);
                return (Integer) read.invoke(null, jar);
            } catch (Exception ex) { return 0; }
        }
    }
    public static class Discover extends Command {
        public JsonObject run(JsonObject cmd) {
            nl.bramstout.mcworldexporter.launcher.LauncherRegistry.initLaunchers();
            List<nl.bramstout.mcworldexporter.launcher.Launcher> launchers = nl.bramstout.mcworldexporter.launcher.LauncherRegistry.getLaunchers();
            launchers.removeIf(l -> l.getName().toLowerCase(Locale.ROOT).contains("hytale"));
            for (JsonElement element : cmd.getAsJsonArray("roots")) {
                JsonObject root = element.getAsJsonObject();
                java.io.File folder = new java.io.File(root.get("path").getAsString());
                if (!folder.isDirectory()) continue;
                final String label = root.get("label").getAsString();
                String kind = root.get("kind").getAsString();
                if (kind.equals("multimc")) launchers.add(new nl.bramstout.mcworldexporter.launcher.LauncherMultiMC(folder) {
                    public String getName() { return label; }
                });
                else if (kind.equals("java")) launchers.add(new nl.bramstout.mcworldexporter.launcher.LauncherJavaEdition(folder) {
                    public String getName() { return label; }
                });
                else if (kind.equals("modrinth")) launchers.add(new nl.bramstout.mcworldexporter.launcher.LauncherModrinth(folder));
            }
            return new CommandGetWorlds().run(cmd);
        }
    }
    public static class MapChunks extends Command {
        public JsonObject run(JsonObject cmd) {
            JsonArray rows = new JsonArray();
            for (JsonElement element : cmd.getAsJsonArray("chunks")) {
                JsonObject coordinate = element.getAsJsonObject();
                int x = coordinate.get("chunkX").getAsInt(), z = coordinate.get("chunkZ").getAsInt();
                try {
                    nl.bramstout.mcworldexporter.world.Chunk chunk = MCWorldExporter.getApp().getWorld().getChunk(x,z);
                    if (chunk == null) continue;
                    chunk.load();
                    // CLI getChunk skips height calculation on Bedrock chunks without a stored heightmap.
                    // Use the same full image render requested by MiEx's interactive map.
                    chunk.setFullReRender(true);
                    chunk.setRenderRequested(true);
                    chunk.renderChunkImage();
                    java.awt.image.BufferedImage img = chunk.getChunkImage();
                    if (img == null) continue;
                    JsonObject row = new JsonObject();
                    row.addProperty("chunkX",x); row.addProperty("chunkZ",z);
                    row.addProperty("image",Util.toBase64(img.getRGB(0,0,16,16,null,0,16)));
                    short[] heights = chunk._getHeightmap();
                    if (heights != null) row.addProperty("heightmap",Util.toBase64(heights));
                    rows.add(row);
                } catch (Exception ex) { throw new RuntimeException("Cannot render chunk " + x + ", " + z + ": " + ex, ex); }
            }
            JsonObject result = new JsonObject(); result.add("chunks",rows); return result;
        }
    }
    static String[] strings(JsonObject cmd, String key, String[] defaults) {
        if (!cmd.has(key)) return defaults;
        ArrayList<String> result = new ArrayList<>();
        for (JsonElement el : cmd.getAsJsonArray(key)) result.add(el.getAsString());
        return result.toArray(new String[0]);
    }
    static final String[] UTILITY = {"_emission", "_emissionMask", "_bump", "_normal", "_specular", "_roughness", "_metalness", "_n", "_s"};
    public static class Atlas extends Command {
        public JsonObject run(JsonObject cmd) {
            // Upstream CLI casts Object[] to String[]; call the same tool with typed arrays.
            nl.bramstout.mcworldexporter.ui.AtlasCreatorDialog.createAtlases(
                Arrays.asList(strings(cmd,"resourcePacks",new String[0])), cmd.get("saveToResourcePack").getAsString(),
                cmd.get("repeats").getAsInt(), cmd.get("padding").getAsInt(),
                strings(cmd,"excludeTextures",new String[0]), strings(cmd,"utilityTextures",UTILITY));
            return new JsonObject();
        }
    }
    public static class Pbr extends Command {
        public JsonObject run(JsonObject cmd) {
            nl.bramstout.mcworldexporter.pbr.PbrGenerator generator = new nl.bramstout.mcworldexporter.pbr.PbrGenerator();
            for (String uuid : strings(cmd,"resourcePacks",new String[0]))
                generator.resourcePacks.add(ResourcePacks.getResourcePack(uuid));
            java.io.File folder = new java.io.File(FileUtil.getResourcePackDir(),cmd.get("saveToResourcePack").getAsString());
            folder.mkdirs();
            generator.saveToResourcePack = new nl.bramstout.mcworldexporter.resourcepack.java.ResourcePackJavaEdition(folder);
            generator.utilitySuffixes.addAll(Arrays.asList(strings(cmd,"utilityTextures",UTILITY)));
            try { generator.init(); generator.process(); }
            catch (Exception ex) { throw new RuntimeException("PBR generation failed", ex); }
            ResourcePacks.load();
            return new JsonObject();
        }
    }
    public static class Packs extends Command {
        public JsonObject run(JsonObject cmd) {
            JsonArray packs = new JsonArray();
            for (ResourcePack pack : ResourcePacks.getResourcePacks()) {
                JsonObject row = new JsonObject();
                row.addProperty("uuid", pack.getUUID());
                row.addProperty("name", pack.getName());
                row.addProperty("folder", pack.getFolder().getAbsolutePath());
                packs.add(row);
            }
            JsonObject result = new JsonObject();
            result.add("resourcePacks", packs);
            result.addProperty("bridgeVersion", 1);
            return result;
        }
    }
    public static class Configure extends Command {
        static ExportBounds readBounds(JsonObject region, String fallbackName) {
            ExportBounds bounds = new ExportBounds(region.has("name") ? region.get("name").getAsString() : fallbackName);
            // Upstream fromJson ignores primitive ints/bools and passes the
            // wrong nested object. Set the supported, validated fields directly.
            bounds.minX = region.get("minX").getAsInt();
            bounds.minY = region.get("minY").getAsInt();
            bounds.minZ = region.get("minZ").getAsInt();
            bounds.maxX = region.get("maxX").getAsInt();
            bounds.maxY = region.get("maxY").getAsInt();
            bounds.maxZ = region.get("maxZ").getAsInt();
            if (bounds.minX > bounds.maxX || bounds.minY > bounds.maxY || bounds.minZ > bounds.maxZ)
                throw new RuntimeException("Invalid export bounds");
            bounds.offsetX = region.get("offsetX").getAsInt();
            bounds.offsetY = region.get("offsetY").getAsInt();
            bounds.offsetZ = region.get("offsetZ").getAsInt();
            bounds.actAsExcludeRegion = false;
            bounds.defaultValues = false;
            bounds.enableLOD = false;
            for (String key : new String[]{"enableLOD","lodCenterX","lodCenterZ","lodWidth","lodDepth",
                    "lodYDetail","lodBaseLevel","onlyIndividualBlocks","chunkSize","excludeRegions","fgChunks"}) {
                if (region.has(key)) {
                    try {
                        Field field = ExportBounds.class.getField(key);
                        field.set(bounds, new Gson().fromJson(region.get(key), field.getGenericType()));
                    } catch (Exception ex) { throw new RuntimeException("Invalid region setting: " + key, ex); }
                }
            }
            return bounds;
        }
        public JsonObject run(JsonObject cmd) {
            if (MCWorldExporter.getApp().getWorld() == null)
                throw new RuntimeException("No world loaded");
            JsonObject region = cmd.getAsJsonObject("region");
            ExportBounds bounds = readBounds(region, "MCAddon");
            ExportData data = ExportData.fromApp();
            data.exportRegions = new ArrayList<ExportBounds>();
            data.exportRegions.add(bounds);
            if (cmd.has("extraRegions")) {
                int i = 2;
                for (JsonElement el : cmd.getAsJsonArray("extraRegions"))
                    data.exportRegions.add(readBounds(el.getAsJsonObject(), "Selection " + i++));
            }
            data.removeCaves = cmd.get("removeCaves").getAsBoolean();
            data.exportBlockAnimations = cmd.get("blockAnimations").getAsBoolean();
            data.entitySpawnRules.clear();
            data.entitySimulate.clear();
            if (!cmd.get("entities").getAsBoolean()) data.entityExport.clear();
            if (cmd.has("options")) {
                JsonObject options = cmd.getAsJsonObject("options");
                for (String key : new String[]{"chunkSize","runOptimiser","fillInCaves","entityStartFrame",
                        "entityEndFrame","entityFPS","entityRandomSeed","entitySpawnDensity","entitySunLightLevel",
                        "entityExport","entitySimulate","entitySpawnRules"}) {
                    if (options.has(key)) {
                        try {
                            Field field = ExportData.class.getField(key);
                            field.set(data, new Gson().fromJson(options.get(key), field.getGenericType()));
                        } catch (Exception ex) { throw new RuntimeException("Invalid export setting: " + key, ex); }
                    }
                }
            }
            data.apply(true);
            // setExportBounds recenters offsets as a UI convenience. Restore
            // the explicit placement requested by Blender after that update.
            List<ExportBounds> all = MCWorldExporter.getApp().getExportBoundsList();
            for (int i = 0; i < all.size(); ++i) {
                JsonObject source = i == 0 ? region : cmd.getAsJsonArray("extraRegions").get(i-1).getAsJsonObject();
                all.get(i).offsetX = source.get("offsetX").getAsInt();
                all.get(i).offsetY = source.get("offsetY").getAsInt();
                all.get(i).offsetZ = source.get("offsetZ").getAsInt();
            }
            JsonObject result = new JsonObject();
            result.add("region", new Gson().toJsonTree(MCWorldExporter.getApp().getActiveExportBounds()));
            result.add("regions", new Gson().toJsonTree(all));
            ExportData applied = ExportData.fromApp();
            JsonObject settings = new JsonObject();
            settings.addProperty("chunkSize", applied.chunkSize);
            settings.addProperty("runOptimiser", applied.runOptimiser);
            settings.addProperty("removeCaves", applied.removeCaves);
            settings.addProperty("fillInCaves", applied.fillInCaves);
            settings.addProperty("blockAnimations", applied.exportBlockAnimations);
            settings.addProperty("entityStartFrame", applied.entityStartFrame);
            settings.addProperty("entityEndFrame", applied.entityEndFrame);
            settings.addProperty("entityFPS", applied.entityFPS);
            settings.addProperty("entityRandomSeed", applied.entityRandomSeed);
            settings.addProperty("entitySpawnDensity", applied.entitySpawnDensity);
            settings.addProperty("entitySunLightLevel", applied.entitySunLightLevel);
            settings.add("entityExport", new Gson().toJsonTree(applied.entityExport));
            settings.add("entitySimulate", new Gson().toJsonTree(applied.entitySimulate));
            settings.add("entitySpawnRules", new Gson().toJsonTree(applied.entitySpawnRules));
            result.add("settings", settings);
            return result;
        }
    }
}
