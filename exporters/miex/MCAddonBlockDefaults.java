// MCAddon compatibility for Minecraft 26.3's compact Java block states.
// Retained for cached base packs created before MiEx experimental-26 added defaults.
// Defaults are generated from Minecraft's own blocks.json report, not guessed
// from resource-pack model variants (whose ordering has no default semantics).
import com.google.gson.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.lang.reflect.Field;
import java.util.Map;
import nl.bramstout.mcworldexporter.nbt.*;
import nl.bramstout.mcworldexporter.world.Chunk;
import nl.bramstout.mcworldexporter.world.anvil.chunkreader.*;

public final class MCAddonBlockDefaults extends ChunkReader_2844_UP {
    private static final JsonObject DEFAULTS = loadDefaults();

    private static JsonObject loadDefaults() {
        try (InputStream stream = MCAddonBlockDefaults.class.getResourceAsStream("/block_defaults_26_3.json")) {
            if (stream == null) throw new IOException("Missing Minecraft block defaults");
            return JsonParser.parseReader(new InputStreamReader(stream, StandardCharsets.UTF_8)).getAsJsonObject();
        } catch (IOException ex) { throw new IllegalStateException(ex); }
    }

    public static void install() {
        try {
            Field field = ChunkReader.class.getDeclaredField("readers");
            field.setAccessible(true);
            ChunkReader[] readers = (ChunkReader[]) field.get(null);
            for (int i = 0; i < readers.length; i++) {
                if (readers[i].getClass() == ChunkReader_2844_UP.class) {
                    readers[i] = new MCAddonBlockDefaults();
                    return;
                }
            }
            throw new IllegalStateException("Unsupported MiEx chunk reader; expected experimental-26");
        } catch (ReflectiveOperationException ex) { throw new IllegalStateException(ex); }
    }

    static NbtTag normalize(NbtTag entry) {
        NbtTagCompound block;
        String name;
        if (entry instanceof NbtTagString) {
            name = entry.asString();
            block = NbtTagCompound.newInstance(entry.getName());
            block.addElement(NbtTagString.newInstance("id", name));
        } else if (entry instanceof NbtTagCompound) {
            block = (NbtTagCompound) entry;
            // Leave old-format worlds alone. New NBT heterogeneous lists wrap
            // compact strings in a compound with an empty-name string tag.
            if (block.get("Name") != null) return entry;
            NbtTag id = block.get("id", "");
            if (!(id instanceof NbtTagString)) return entry;
            name = id.asString();
        } else return entry;
        if (!name.contains(":")) name = "minecraft:" + name;
        JsonObject defaults = DEFAULTS.has(name) ? DEFAULTS.getAsJsonObject(name) : null;
        if (defaults != null) {
            NbtTagCompound properties = (NbtTagCompound) block.get("Properties", "properties");
            if (properties == null) {
                properties = NbtTagCompound.newInstance("properties");
                block.addElement(properties);
            }
            for (Map.Entry<String, JsonElement> value : defaults.entrySet()) {
                if (properties.get(value.getKey()) == null)
                    properties.addElement(NbtTagString.newInstance(value.getKey(), value.getValue().getAsString()));
            }
        }
        return block;
    }

    @Override public void readChunk(Chunk chunk, NbtTagCompound root, int dataVersion) {
        NbtTagList sections = (NbtTagList) root.get("sections");
        if (sections != null) for (NbtTag section : sections.getData()) {
            NbtTagCompound states = (NbtTagCompound) ((NbtTagCompound) section).get("block_states");
            if (states == null) continue;
            NbtTagList palette = (NbtTagList) states.get("palette");
            if (palette == null) continue;
            NbtTag[] entries = new NbtTag[palette.getSize()];
            for (int i = 0; i < entries.length; i++) {
                NbtTag old = palette.get(i).copy();
                entries[i] = normalize(old);
                if (entries[i] != old) old.free();
            }
            palette.setData(entries);
        }
        super.readChunk(chunk, root, dataVersion);
    }
}
