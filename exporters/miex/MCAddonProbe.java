// MCAddon diagnostics for MiEx 2.0 experimental-28. Original MCAddon code.
// Reports what MiEx resolves at world positions: the block after translation,
// after neighbour connections, the baked models' textures and the pack that
// supplies each texture, the tint and the biome colours. Tests use it to check
// a world import block by block without exporting or opening Blender.
import com.google.gson.*;
import java.util.*;
import nl.bramstout.mcworldexporter.MCWorldExporter;
import nl.bramstout.mcworldexporter.commands.Command;
import nl.bramstout.mcworldexporter.model.*;
import nl.bramstout.mcworldexporter.nbt.NbtTagCompound;
import nl.bramstout.mcworldexporter.resourcepack.*;
import nl.bramstout.mcworldexporter.translation.BlockConnectionsTranslation;
import nl.bramstout.mcworldexporter.world.*;

public class MCAddonProbe extends Command {
    public JsonObject run(JsonObject cmd) {
        World world = MCWorldExporter.getApp().getWorld();
        if (world == null) throw new RuntimeException("No world loaded");
        JsonArray rows = new JsonArray();
        for (JsonElement element : cmd.getAsJsonArray("positions")) {
            JsonArray p = element.getAsJsonArray();
            int x = p.get(0).getAsInt(), y = p.get(1).getAsInt(), z = p.get(2).getAsInt();
            JsonObject row = new JsonObject();
            row.add("pos", p);
            JsonArray layers = new JsonArray();
            LayeredBlock blocks = new LayeredBlock();
            world.getBlockId(x, y, z, blocks);
            for (int layer = 0; layer < blocks.getLayerCount(); layer++) {
                try { layers.add(probe(world, blocks.getBlock(layer), x, y, z, layer)); }
                catch (Exception ex) {
                    JsonObject failed = new JsonObject();
                    failed.addProperty("error", String.valueOf(ex));
                    layers.add(failed);
                }
            }
            row.add("layers", layers);
            int biomeId = world.getBiomeId(x, y, z);
            Biome biome = BiomeRegistry.getBiome(biomeId);
            JsonObject biomeRow = new JsonObject();
            biomeRow.addProperty("name", biome == null ? null : biome.getName());
            JsonObject colours = new JsonObject();
            if (biome != null) for (Map.Entry<String, nl.bramstout.mcworldexporter.Color> c : biome.getColors())
                if (c.getValue() != null) colours.add(c.getKey(), colour(c.getValue()));
            biomeRow.add("colours", colours);
            row.add("biome", biomeRow);
            rows.add(row);
        }
        JsonObject result = new JsonObject();
        result.add("blocks", rows);
        result.add("missingBlockStates", new Gson().toJsonTree(new ArrayList<>(BlockStateRegistry.missingBlockStates)));
        result.add("missingBiomes", new Gson().toJsonTree(new ArrayList<>(BiomeRegistry.missingBiomes)));
        return result;
    }

    static JsonObject probe(World world, int blockId, int x, int y, int z, int layer) throws Exception {
        Block block = BlockRegistry.getBlock(blockId);
        JsonObject out = new JsonObject();
        out.addProperty("name", block.getName());
        out.add("props", block.getProperties() == null ? new JsonObject() : block.getProperties().asJson());
        int stateId = BlockStateRegistry.getIdForName(block.getName(), block.getDataVersion());
        BlockState state = BlockStateRegistry.getState(stateId);
        out.addProperty("handler", state.getHandler() == null ? null : state.getHandler().getClass().getSimpleName());
        java.lang.reflect.Field field = BlockState.class.getDeclaredField("blockConnections");
        field.setAccessible(true);
        BlockConnectionsTranslation.BlockConnections connections = (BlockConnectionsTranslation.BlockConnections) field.get(state);
        if (connections != null && block.getProperties() != null) {
            NbtTagCompound copy = (NbtTagCompound) block.getProperties().copy();
            String renamed = connections.map(block.getName(), copy, x, y, z, layer);
            out.addProperty("connectedName", renamed == null ? block.getName() : renamed);
            out.add("connectedProps", copy.asJson());
            copy.free();
        }
        BakedBlockState baked = BlockStateRegistry.getBakedStateForBlock(blockId, x, y, z, layer, true, false);
        out.addProperty("bakedName", baked.getName());
        List<Model> models = new ArrayList<>();
        baked.getModels(x, y, z, models);
        out.addProperty("models", models.size());
        TreeMap<String, String> textures = new TreeMap<>();
        int faces = 0;
        float[] points = new float[12];
        double[] centre = new double[3];
        for (Model model : models) {
            for (ModelFace face : model.getFaces()) {
                faces++;
                face.getPoints(points);
                for (int i = 0; i < 12; i++) centre[i % 3] += points[i] / 4.0;
                String texture = model.getTexture(face.getTexture());
                if (texture == null || textures.containsKey(texture)) continue;
                java.io.File file = ResourcePacks.getTexture(texture);
                textures.put(texture, file == null ? null : file.getAbsolutePath());
            }
        }
        out.addProperty("faces", faces);
        // Average face centre in 1/16-block units: shows which way a model leans or points.
        JsonArray middle = new JsonArray();
        for (int i = 0; i < 3; i++) middle.add(faces == 0 ? 0.0 : Math.round(centre[i] / faces * 100.0) / 100.0);
        out.add("centre", middle);
        JsonObject textureRows = new JsonObject();
        for (Map.Entry<String, String> t : textures.entrySet()) textureRows.addProperty(t.getKey(), t.getValue());
        out.add("textures", textureRows);
        out.add("tint", baked.getTint() == null ? null : baked.getTint().toJson());
        return out;
    }

    static JsonArray colour(nl.bramstout.mcworldexporter.Color c) {
        JsonArray rgb = new JsonArray();
        rgb.add(c.r); rgb.add(c.g); rgb.add(c.b);
        return rgb;
    }
}
