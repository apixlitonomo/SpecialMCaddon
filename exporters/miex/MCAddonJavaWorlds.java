// Sparkle's Java-world fidelity fixes for MiEx 2.0 experimental-28.
// Original MCAddon code. MiEx.jar stays unmodified; everything here works through
// reflection on MiEx's own registries, its command registry and its data files.
import com.google.gson.*;
import java.io.*;
import java.lang.reflect.*;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.zip.*;
import nl.bramstout.mcworldexporter.*;
import nl.bramstout.mcworldexporter.commands.*;
import nl.bramstout.mcworldexporter.entity.EntityRegistry;
import nl.bramstout.mcworldexporter.export.ExportData;
import nl.bramstout.mcworldexporter.model.BlockStateRegistry;
import nl.bramstout.mcworldexporter.model.ModelRegistry;
import nl.bramstout.mcworldexporter.resourcepack.*;
import nl.bramstout.mcworldexporter.world.BiomeRegistry;

public final class MCAddonJavaWorlds {
    private MCAddonJavaWorlds() {}

    /** Called from MCAddonMiEx.main before MiEx starts. Each part fails on its own. */
    public static void install(String[] args) {
        try { BuiltIns.install(args); }
        catch (Throwable ex) { System.out.println("Sparkle could not install MiEx built-in files: " + ex); }
        try { BiomeFallback.install(); }
        catch (Throwable ex) { System.out.println("Sparkle could not install the biome fallback: " + ex); }
        try { MCAddonJavaPacks.install(); }
        catch (Throwable ex) { System.out.println("Sparkle could not install its Java item models: " + ex); }
        CommandRegistry.registerCommand("getExportSettings", ExportSettings.class);
        CommandRegistry.registerCommand("mcaddonMap", MapChunks.class);
        CommandRegistry.registerCommand("mcaddonProblems", Problems.class);
    }

    // MiEx installs default_data/ from its jar at start-up, but finds the jar with
    // new File(url.getPath()): "%20" stays in the path, so on any path with a space
    // ("Blender Foundation") nothing is installed and manifest.json stays {}. Then
    // item frames, paintings, armour stands, displays, chests, beds, signs, heads and
    // shulker boxes have no built-in model, old block and biome names and every
    // Bedrock block have no translation, and the inferred tints colour redstone
    // wire, lily pads and stems like grass. Install the same files from the decoded
    // jar path, with MiEx's own manifest hashes. Locators, lighting and material
    // templates are left out so Sparkle's exports keep their materials and objects.
    static final class BuiltIns {
        static final String[] FOLDERS = {"base_resource_pack/builtins/", "base_resource_pack/translation/"};
        static final Set<String> FILES = new HashSet<>(Arrays.asList(
            "base_resource_pack/miex_block_tints.json", "base_resource_pack/miex_patches.json"));

        static boolean wanted(String name) {
            if (FILES.contains(name)) return true;
            for (String folder : FOLDERS) if (name.startsWith(folder)) return true;
            return false;
        }

        static File resourceDir(String[] args) {
            for (int i = 0; i + 1 < args.length; i++)
                if (args[i].equalsIgnoreCase("-rpDir")) return new File(args[i + 1]);
            String env = System.getenv("MIEX_RESOURCEPACK_DIR");
            return new File(env != null ? env : "./resources");
        }

        static int install(String[] args) throws Exception {
            URL location = MCWorldExporter.class.getProtectionDomain().getCodeSource().getLocation();
            File jar = new File(location.toURI());
            if (!jar.isFile()) return 0;
            File root = resourceDir(args);
            root.mkdirs();
            Method hashMethod = BuiltInFiles.class.getDeclaredMethod("calcHash", InputStream.class, long.class);
            hashMethod.setAccessible(true);
            File manifestFile = new File(root, "manifest.json");
            JsonObject manifest = null;
            if (manifestFile.isFile()) {
                try (Reader reader = new InputStreamReader(new FileInputStream(manifestFile), StandardCharsets.UTF_8)) {
                    JsonElement element = JsonParser.parseReader(reader);
                    if (element.isJsonObject()) manifest = element.getAsJsonObject();
                } catch (Exception ex) { manifest = null; }
            }
            if (manifest == null) manifest = new JsonObject();
            int installed = 0;
            try (ZipFile zip = new ZipFile(jar)) {
                for (Enumeration<? extends ZipEntry> it = zip.entries(); it.hasMoreElements();) {
                    ZipEntry entry = it.nextElement();
                    if (entry.isDirectory() || !entry.getName().startsWith("default_data/")) continue;
                    String name = entry.getName().substring("default_data/".length());
                    if (!wanted(name)) continue;
                    String hash;
                    try (InputStream in = zip.getInputStream(entry)) { hash = (String) hashMethod.invoke(null, in, entry.getSize()); }
                    JsonElement stored = manifest.get(name);
                    String storedHash = stored != null && stored.isJsonObject() && stored.getAsJsonObject().has("hash")
                        ? stored.getAsJsonObject().get("hash").getAsString() : "";
                    if (storedHash.equalsIgnoreCase("NO_OVERWRITE")) continue;
                    File target = new File(root, name);
                    if (storedHash.equals(hash) && target.isFile()) continue;
                    if (!storedHash.isEmpty() && target.isFile()) {
                        String actual;
                        try (InputStream in = new FileInputStream(target)) { actual = (String) hashMethod.invoke(null, in, target.length()); }
                        // Edited by hand since MiEx installed it: keep it, as MiEx does.
                        if (!actual.equals(storedHash) && !actual.equals(hash)) continue;
                    }
                    target.getParentFile().mkdirs();
                    try (InputStream in = zip.getInputStream(entry)) {
                        Files.copy(in, target.toPath(), StandardCopyOption.REPLACE_EXISTING);
                    }
                    JsonObject row = new JsonObject();
                    row.addProperty("hash", hash);
                    manifest.add(name, row);
                    installed++;
                }
            }
            if (installed > 0) {
                try (Writer writer = new OutputStreamWriter(new FileOutputStream(manifestFile), StandardCharsets.UTF_8)) {
                    new GsonBuilder().setPrettyPrinting().create().toJson(manifest, writer);
                }
                System.out.println("Sparkle installed " + installed + " MiEx built-in files.");
            }
            return installed;
        }
    }

    // A biome that no active pack defines (custom server biomes, a download without
    // its datapack) had no colours at all, so grass, leaves and water stayed the
    // grey of their textures. Minecraft reads an unknown biome as plains; fill each
    // colour a biome lacks from MiEx's own minecraft:plains for the active packs.
    // BiomeRegistry is shared by Java and Bedrock worlds.
    static final class BiomeFallback {
        static final Set<String> filled = Collections.synchronizedSet(new TreeSet<>());
        private static String plainsKey = null;
        private static Map<String, Color> plains = null;

        static final class Plains extends Biome {
            Plains(String name, int id, Map<String, Color> colours) {
                super(name, id);
                for (Map.Entry<String, Color> entry : colours.entrySet()) setColor(entry.getKey(), new Color(entry.getValue()));
            }
            public void calculateTints() {}
        }

        static final class Biomes extends ArrayList<Biome> {
            Biomes(Collection<Biome> existing) { super(existing); for (Biome biome : this) fill(biome); }
            @Override public boolean add(Biome biome) { fill(biome); return super.add(biome); }
            @Override public Biome set(int index, Biome biome) { fill(biome); return super.set(index, biome); }
        }

        static void install() throws Exception {
            Field field = BiomeRegistry.class.getDeclaredField("registeredBiomes");
            field.setAccessible(true);
            @SuppressWarnings("unchecked") List<Biome> current = (List<Biome>) field.get(null);
            if (!(current instanceof Biomes)) field.set(null, new Biomes(current));
        }

        static synchronized Map<String, Color> plains() {
            StringBuilder key = new StringBuilder();
            for (ResourcePack pack : ResourcePacks.getActiveResourcePacks())
                key.append(pack.getUUID()).append('@').append(System.identityHashCode(pack)).append(';');
            if (plains != null && key.toString().equals(plainsKey)) return plains;
            Map<String, Color> colours = new LinkedHashMap<>();
            Biome biome = ResourcePacks.getBiome("minecraft:plains", -1, false);
            for (Map.Entry<String, Color> entry : biome.getColors())
                if (entry.getValue() != null) colours.put(entry.getKey(), entry.getValue());
            // Vanilla plains when no pack defines it (temperature .8, downfall .4).
            colours.putIfAbsent("minecraft:grass", new Color(0x91BD59));
            colours.putIfAbsent("minecraft:foliage", new Color(0x77AB2F));
            colours.putIfAbsent("minecraft:water", new Color(0x3F76E4));
            plains = colours;
            plainsKey = key.toString();
            return colours;
        }

        static void fill(Biome biome) {
            if (!(biome instanceof BiomeCombined) || "minecraft:the_void".equals(biome.getName())) return;
            try {
                BiomeCombined combined = (BiomeCombined) biome;
                combined.getSubBiomes().removeIf(sub -> sub instanceof Plains);
                Map<String, Color> missing = new LinkedHashMap<>();
                for (Map.Entry<String, Color> entry : plains().entrySet())
                    if (combined.getColor(entry.getKey()) == null) missing.put(entry.getKey(), entry.getValue());
                if (missing.isEmpty()) return;
                combined.getSubBiomes().add(new Plains(biome.getName(), biome.getId(), missing));
                combined.calculateTints();
                if (combined.getSubBiomes().size() == 1) filled.add(biome.getName());
            } catch (Throwable ex) {
                System.out.println("Sparkle biome fallback failed for " + biome.getName() + ": " + ex);
            }
        }
    }

    // Upstream's getExportSettings serialises ExportBounds field by field and casts
    // every list's element type to Class: List<Pair<Integer, Integer>> throws
    // (ClassCastException, logged as "An error happened somewhere") and every int,
    // float and boolean is skipped. Serialise the same keys completely with Gson.
    public static class ExportSettings extends Command {
        public JsonObject run(JsonObject cmd) {
            ExportData data = ExportData.fromApp();
            Gson gson = new Gson();
            JsonObject settings = new JsonObject();
            for (Field field : ExportData.class.getFields()) {
                if (Modifier.isStatic(field.getModifiers())) continue;
                try {
                    if (field.getName().equals("exportRegions")) {
                        JsonObject regions = new JsonObject();
                        for (ExportBounds bounds : data.exportRegions) regions.add(bounds.getName(), gson.toJsonTree(bounds));
                        settings.add("exportRegions", regions);
                    } else if (field.getName().equals("resourcePacks")) {
                        JsonArray ids = new JsonArray();
                        for (ResourcePack pack : data.resourcePacks) if (pack != null) ids.add(pack.getUUID());
                        settings.add("resourcePacks", ids);
                    } else {
                        settings.add(field.getName(), gson.toJsonTree(field.get(data)));
                    }
                } catch (Exception ex) {
                    System.out.println("Sparkle skipped export setting " + field.getName() + ": " + ex);
                }
            }
            JsonObject result = new JsonObject();
            result.add("settings", settings);
            return result;
        }
    }

    // MCAddonMiEx.MapChunks failed a whole request when one chunk threw, and the map
    // then stopped asking for tiles. A chunk that cannot be drawn is left out (as an
    // absent chunk is) and named in "failed" and "warnings"; the rest still draw.
    public static class MapChunks extends Command {
        public JsonObject run(JsonObject cmd) {
            JsonArray requested = cmd.getAsJsonArray("chunks");
            final int count = requested.size();
            final JsonObject[] rows = new JsonObject[count];
            final String[] errors = new String[count];
            Map<Long, Integer> first = new HashMap<>();
            int[] sameAs = new int[count];
            List<nl.bramstout.mcworldexporter.parallel.ThreadPool.Task> tasks = new ArrayList<>();
            for (int i = 0; i < count; i++) {
                JsonObject coordinate = requested.get(i).getAsJsonObject();
                final int x = coordinate.get("chunkX").getAsInt(), z = coordinate.get("chunkZ").getAsInt();
                Integer earlier = first.putIfAbsent(((long) x << 32) ^ (z & 0xffffffffL), i);
                sameAs[i] = earlier == null ? i : earlier;
                if (earlier != null) continue;
                final int index = i;
                tasks.add(nl.bramstout.mcworldexporter.ui.Renderer2D.threadPool.submit(() -> {
                    try { rows[index] = MCAddonMiEx.MapChunks.mapChunk(x, z); }
                    catch (Throwable ex) {
                        ex.printStackTrace(System.out);
                        errors[index] = "Chunk " + x + ", " + z + " could not be drawn: " + ex;
                    }
                }));
            }
            for (nl.bramstout.mcworldexporter.parallel.ThreadPool.Task task : tasks) task.waitUntilTaskIsDoneFast();
            JsonArray out = new JsonArray(), failed = new JsonArray(), warnings = new JsonArray();
            for (int i = 0; i < count; i++) {
                if (sameAs[i] != i) continue;
                if (rows[i] != null) out.add(rows[i]);
                if (errors[i] != null) {
                    JsonArray key = new JsonArray();
                    key.add(requested.get(i).getAsJsonObject().get("chunkX"));
                    key.add(requested.get(i).getAsJsonObject().get("chunkZ"));
                    failed.add(key);
                    if (warnings.size() < 3) warnings.add(errors[i]);
                }
            }
            JsonObject result = new JsonObject();
            result.add("chunks", out);
            if (failed.size() > 0) { result.add("failed", failed); result.add("warnings", warnings); }
            return result;
        }
    }

    // What MiEx could not find since the packs were last applied, for one short
    // warning in Blender instead of a line in the log nobody reads.
    public static class Problems extends Command {
        /** A MiEx public static list, read by reflection so a renamed one is skipped, not fatal. */
        static JsonArray copy(Class<?> owner, String field) {
            JsonArray out = new JsonArray();
            try {
                Object value = owner.getField(field).get(null);
                if (value instanceof List) {
                    List<?> list = (List<?>) value;
                    synchronized (list) { for (Object name : new TreeSet<>(list)) out.add(String.valueOf(name)); }
                }
            } catch (Throwable ex) {
                System.out.println("Sparkle cannot read MiEx's " + owner.getSimpleName() + "." + field + ": " + ex);
            }
            return out;
        }
        public JsonObject run(JsonObject cmd) {
            JsonObject result = new JsonObject();
            result.add("models", copy(ModelRegistry.class, "missingModels"));
            result.add("blockstates", copy(BlockStateRegistry.class, "missingBlockStates"));
            result.add("entities", copy(EntityRegistry.class, "missingEntities"));
            JsonArray biomes = new JsonArray();
            synchronized (BiomeFallback.filled) { for (String name : BiomeFallback.filled) biomes.add(name); }
            result.add("biomes", biomes);
            return result;
        }
    }
}
