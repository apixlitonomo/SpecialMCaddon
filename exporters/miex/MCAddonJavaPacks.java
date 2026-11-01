// Java resource-pack item models for MiEx 2.0 experimental-28, evaluated as Minecraft does.
// Original MCAddon code. MiEx.jar stays unmodified: its ResourcePackJavaEdition
// instances are replaced by a subclass as MiEx creates them, and only item model
// lookups change. "special" models and any unknown type still go to MiEx.
import com.google.gson.*;
import java.awt.image.BufferedImage;
import java.io.File;
import java.lang.reflect.Field;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import nl.bramstout.mcworldexporter.Color;
import nl.bramstout.mcworldexporter.Json;
import nl.bramstout.mcworldexporter.math.Matrix;
import nl.bramstout.mcworldexporter.model.Model;
import nl.bramstout.mcworldexporter.model.ModelRegistry;
import nl.bramstout.mcworldexporter.nbt.*;
import nl.bramstout.mcworldexporter.resourcepack.*;
import nl.bramstout.mcworldexporter.resourcepack.java.ItemHandlerJavaEdition;
import nl.bramstout.mcworldexporter.resourcepack.java.ResourcePackJavaEdition;

final class MCAddonJavaPacks {
    private MCAddonJavaPacks() {}

    /** ResourcePacks keeps one list of every pack; its additions become JavaPacks. */
    static void install() throws Exception {
        Field field = ResourcePacks.class.getDeclaredField("resourcePacks");
        field.setAccessible(true);
        @SuppressWarnings("unchecked") List<ResourcePack> current = (List<ResourcePack>) field.get(null);
        if (!(current instanceof Packs)) field.set(null, new Packs(current));
    }

    static ResourcePack wrap(ResourcePack pack) {
        if (pack == null || pack.getClass() != ResourcePackJavaEdition.class) return pack;
        try { return new JavaPack(pack.getFolder()); }
        catch (Throwable ex) { Items.report("Kept MiEx's pack " + pack.getName() + ": " + ex); return pack; }
    }

    static final class Packs extends ArrayList<ResourcePack> {
        Packs(Collection<ResourcePack> existing) { for (ResourcePack pack : existing) add(pack); }
        @Override public boolean add(ResourcePack pack) { return super.add(wrap(pack)); }
        @Override public void add(int index, ResourcePack pack) { super.add(index, wrap(pack)); }
        @Override public ResourcePack set(int index, ResourcePack pack) { return super.set(index, wrap(pack)); }
        @Override public boolean addAll(Collection<? extends ResourcePack> packs) {
            for (ResourcePack pack : packs) add(pack);
            return !packs.isEmpty();
        }
    }

    static final class JavaPack extends ResourcePackJavaEdition {
        JavaPack(File folder) { super(folder); }

        @Override public ItemHandler getItemHandler(String name, NbtTagCompound data) {
            try {
                String item = Items.modelName(name, data);
                File file = getResource(item, "items", "assets", ".json");
                if (file != null && file.isFile()) {
                    Items.Node root = Items.definition(file);
                    if (root != null) return new Items.Handler(root);
                } else {
                    int colon = item.indexOf(':');
                    String namespace = colon >= 0 ? item.substring(0, colon) : "minecraft";
                    String path = colon >= 0 ? item.substring(colon + 1) : item;
                    for (String folder : new String[] {":item/", ":block/"}) {
                        File legacy = getResource(namespace + folder + path, "models", "assets", ".json");
                        if (legacy == null || !legacy.isFile()) continue;
                        Items.Legacy overrides = Items.legacy(namespace + folder + path, legacy);
                        if (overrides != null) return new Items.LegacyHandler(overrides);
                        break;
                    }
                }
            } catch (Throwable ex) {
                Items.report("Item model " + name + " in " + getName() + ": " + ex);
            }
            return super.getItemHandler(name, data);
        }
    }

    static final class Items {
        private static final Map<String, Object[]> cache = new ConcurrentHashMap<>();
        private static final Set<String> reported = Collections.synchronizedSet(new HashSet<>());

        static void report(String message) {
            if (reported.add(message)) System.out.println("Sparkle: " + message);
        }

        static String modelName(String name, NbtTagCompound data) {
            NbtTag model = component(data, "minecraft:item_model");
            String result = model instanceof NbtTagString ? model.asString() : name;
            return result.contains(":") ? result : "minecraft:" + result;
        }

        /** Parsed once per file version: OR's paper.json alone is 9,000 entries. */
        static Node definition(File file) {
            String key = file.getAbsolutePath();
            Object[] hit = cache.get(key);
            if (hit != null && (Long) hit[0] == file.lastModified() && (Long) hit[1] == file.length()) return (Node) hit[2];
            JsonElement json = Json.read(file);
            Node node = json != null && json.isJsonObject() && json.getAsJsonObject().get("model") instanceof JsonObject
                ? parse(json.getAsJsonObject().getAsJsonObject("model")) : null;
            if (node == null) report("Unreadable item model definition " + file);
            cache.put(key, new Object[] {file.lastModified(), file.length(), node});
            return node;
        }

        static Legacy legacy(String name, File file) {
            String key = "legacy:" + file.getAbsolutePath();
            Object[] hit = cache.get(key);
            if (hit != null && (Long) hit[0] == file.lastModified() && (Long) hit[1] == file.length()) return (Legacy) hit[2];
            JsonElement json = Json.read(file);
            Legacy legacy = null;
            if (json != null && json.isJsonObject() && json.getAsJsonObject().get("overrides") instanceof JsonArray)
                legacy = new Legacy(name, json.getAsJsonObject().getAsJsonArray("overrides"));
            cache.put(key, new Object[] {file.lastModified(), file.length(), legacy});
            return legacy;
        }

        // --- NBT helpers ----------------------------------------------------
        static NbtTag component(NbtTagCompound data, String id) {
            if (data == null) return null;
            NbtTag components = data.get("components");
            if (!(components instanceof NbtTagCompound)) return null;
            NbtTag value = ((NbtTagCompound) components).get(id);
            if (value == null && id.startsWith("minecraft:")) value = ((NbtTagCompound) components).get(id.substring(10));
            return value;
        }

        static NbtTag legacyTag(NbtTagCompound data, String name) {
            NbtTag tag = data == null ? null : data.get("tag");
            return tag instanceof NbtTagCompound ? ((NbtTagCompound) tag).get(name) : null;
        }

        static boolean numeric(NbtTag tag) {
            return tag instanceof NbtTagByte || tag instanceof NbtTagShort || tag instanceof NbtTagInt
                || tag instanceof NbtTagLong || tag instanceof NbtTagFloat || tag instanceof NbtTagDouble;
        }

        static List<NbtTag> list(NbtTag tag) {
            List<NbtTag> out = new ArrayList<>();
            if (tag instanceof NbtTagList) {
                NbtTagList values = (NbtTagList) tag;
                for (int i = 0; i < values.getSize(); i++) out.add(values.get(i));
            } else if (tag instanceof NbtTagIntArray) {
                for (int value : ((NbtTagIntArray) tag).getData()) out.add(NbtTagInt.newNonPooledInstance("", value));
            }
            return out;
        }

        /** custom_model_data: {floats, flags, strings, colors}; an int is the pre-1.21.4 form. */
        static List<NbtTag> customModelData(NbtTagCompound data, String field) {
            NbtTag cmd = component(data, "minecraft:custom_model_data");
            if (cmd instanceof NbtTagCompound) return list(((NbtTagCompound) cmd).get(field));
            if (numeric(cmd) && field.equals("floats")) return Collections.singletonList(cmd);
            NbtTag old = legacyTag(data, "CustomModelData");
            if (numeric(old) && field.equals("floats")) return Collections.singletonList(old);
            return Collections.emptyList();
        }

        static int intOf(NbtTag tag, int fallback) { return numeric(tag) ? tag.asInt() : fallback; }

        /** An RGB int, or [r, g, b] floats from 0 to 1, as Minecraft's colour codecs read them. */
        static Integer rgb(JsonElement value) {
            if (value == null || value.isJsonNull()) return null;
            if (value.isJsonPrimitive() && value.getAsJsonPrimitive().isNumber()) return value.getAsInt();
            if (value.isJsonArray() && value.getAsJsonArray().size() >= 3) {
                JsonArray array = value.getAsJsonArray();
                int r = Math.round(Math.max(0f, Math.min(1f, array.get(0).getAsFloat())) * 255f);
                int g = Math.round(Math.max(0f, Math.min(1f, array.get(1).getAsFloat())) * 255f);
                int b = Math.round(Math.max(0f, Math.min(1f, array.get(2).getAsFloat())) * 255f);
                return r << 16 | g << 8 | b;
            }
            return null;
        }

        static Integer rgb(NbtTag value) {
            if (numeric(value)) return value.asInt();
            List<NbtTag> values = list(value);
            if (values.size() >= 3) {
                int r = Math.round(Math.max(0f, Math.min(1f, values.get(0).asFloat())) * 255f);
                int g = Math.round(Math.max(0f, Math.min(1f, values.get(1).asFloat())) * 255f);
                int b = Math.round(Math.max(0f, Math.min(1f, values.get(2).asFloat())) * 255f);
                return r << 16 | g << 8 | b;
            }
            return null;
        }

        static String key(String id) { return id == null ? null : id.contains(":") ? id : "minecraft:" + id; }

        // --- Item model definitions (assets/<ns>/items/*.json) --------------
        abstract static class Node {
            abstract void setup(String name, NbtTagCompound data, String context, Model target);
        }

        static Node parse(JsonObject json) {
            if (json == null || !json.has("type")) return null;
            String type = key(json.get("type").getAsString());
            switch (type) {
                case "minecraft:model": return new ModelNode(json);
                case "minecraft:composite": return new Composite(json);
                case "minecraft:condition": return new Condition(json);
                case "minecraft:select": return new Select(json);
                case "minecraft:range_dispatch": return new RangeDispatch(json);
                case "minecraft:empty": case "minecraft:bundle/selected_item": return new Empty();
                default: return new Delegate(json);
            }
        }

        static final class Empty extends Node {
            void setup(String name, NbtTagCompound data, String context, Model target) {}
        }

        /** special and anything newer: MiEx's own evaluation of that subtree. */
        static final class Delegate extends Node {
            final ItemHandlerJavaEdition handler;
            Delegate(JsonObject json) {
                JsonObject wrapper = new JsonObject();
                wrapper.add("model", json);
                handler = new ItemHandlerJavaEdition(wrapper);
            }
            void setup(String name, NbtTagCompound data, String context, Model target) {
                Model model = handler.getModel(name, data, context);
                if (model != null) target.addModel(model);
            }
        }

        static final class ModelNode extends Node {
            final String model;
            final List<Tint> tints = new ArrayList<>();
            ModelNode(JsonObject json) {
                model = json.has("model") ? json.get("model").getAsString() : null;
                if (json.get("tints") instanceof JsonArray)
                    for (JsonElement tint : json.getAsJsonArray("tints"))
                        tints.add(tint.isJsonObject() ? Tint.parse(tint.getAsJsonObject()) : null);
            }
            void setup(String name, NbtTagCompound data, String context, Model target) {
                if (model == null) return;
                Model base = ModelRegistry.getModel(ModelRegistry.getIdForName(model, false));
                if (base == null) return;
                List<Color> colours = new ArrayList<>();
                for (Tint tint : tints) {
                    Integer value = tint == null ? null : tint.rgb(data);
                    colours.add(value == null ? new Color(1f, 1f, 1f, 1f) : new Color(value));
                }
                base = Display.apply(model, base, context);
                target.addModel(base, colours);
            }
        }

        static final class Composite extends Node {
            final List<Node> models = new ArrayList<>();
            Composite(JsonObject json) {
                if (json.get("models") instanceof JsonArray)
                    for (JsonElement model : json.getAsJsonArray("models"))
                        if (model.isJsonObject()) { Node node = parse(model.getAsJsonObject()); if (node != null) models.add(node); }
            }
            void setup(String name, NbtTagCompound data, String context, Model target) {
                for (Node node : models) node.setup(name, data, context, target);
            }
        }

        static final class Condition extends Node {
            final String property;
            final JsonObject json;
            final Node onTrue, onFalse;
            Condition(JsonObject json) {
                this.json = json;
                property = json.has("property") ? key(json.get("property").getAsString()) : "";
                onTrue = parse(json.getAsJsonObject("on_true"));
                onFalse = parse(json.getAsJsonObject("on_false"));
            }
            boolean test(NbtTagCompound data) {
                switch (property) {
                    case "minecraft:custom_model_data": {
                        int index = json.has("index") ? json.get("index").getAsInt() : 0;
                        List<NbtTag> flags = customModelData(data, "flags");
                        return index >= 0 && index < flags.size() && flags.get(index).asInt() != 0;
                    }
                    case "minecraft:has_component":
                        return json.has("component") && component(data, key(json.get("component").getAsString())) != null;
                    case "minecraft:damaged":
                        return intOf(component(data, "minecraft:damage"), intOf(legacyTag(data, "Damage"), 0)) > 0;
                    case "minecraft:broken": {
                        int max = intOf(component(data, "minecraft:max_damage"), 0);
                        int damage = intOf(component(data, "minecraft:damage"), 0);
                        return max > 0 && damage >= max - 1;
                    }
                    default:
                        // using_item, selected, carried, fishing_rod/cast, view_entity, keybind_down ...:
                        // nothing in a frame, display or stand is held, used or looked at.
                        return false;
                }
            }
            void setup(String name, NbtTagCompound data, String context, Model target) {
                Node node = test(data) ? onTrue : onFalse;
                if (node != null) node.setup(name, data, context, target);
            }
        }

        static final class Select extends Node {
            final String property;
            final JsonObject json;
            final Map<String, Node> cases = new LinkedHashMap<>();
            final Node fallback;
            Select(JsonObject json) {
                this.json = json;
                property = json.has("property") ? key(json.get("property").getAsString()) : "";
                if (json.get("cases") instanceof JsonArray)
                    for (JsonElement row : json.getAsJsonArray("cases")) {
                        if (!row.isJsonObject()) continue;
                        JsonObject entry = row.getAsJsonObject();
                        Node node = parse(entry.getAsJsonObject("model"));
                        JsonElement when = entry.get("when");
                        List<JsonElement> values = new ArrayList<>();
                        if (when != null && when.isJsonArray()) when.getAsJsonArray().forEach(values::add);
                        else if (when != null) values.add(when);
                        for (JsonElement value : values) cases.putIfAbsent(caseKey(value), node);
                    }
                fallback = parse(json.getAsJsonObject("fallback"));
            }
            String caseKey(JsonElement value) {
                String text = value.isJsonPrimitive() ? value.getAsString() : value.toString();
                return property.equals("minecraft:trim_material") || property.equals("minecraft:context_entity_type") ? key(text) : text;
            }
            String value(NbtTagCompound data, String context) {
                switch (property) {
                    case "minecraft:custom_model_data": {
                        int index = json.has("index") ? json.get("index").getAsInt() : 0;
                        List<NbtTag> strings = customModelData(data, "strings");
                        return index >= 0 && index < strings.size() ? strings.get(index).asString() : null;
                    }
                    case "minecraft:display_context": return context == null || context.isEmpty() ? "none" : context;
                    case "minecraft:main_hand": return "right";
                    case "minecraft:charge_type": {
                        List<NbtTag> projectiles = list(component(data, "minecraft:charged_projectiles"));
                        if (projectiles.isEmpty()) return "none";
                        for (NbtTag projectile : projectiles) {
                            NbtTag id = projectile instanceof NbtTagCompound ? ((NbtTagCompound) projectile).get("id") : null;
                            if (id != null && "minecraft:firework_rocket".equals(key(id.asString()))) return "rocket";
                        }
                        return "arrow";
                    }
                    case "minecraft:trim_material": {
                        NbtTag trim = component(data, "minecraft:trim");
                        NbtTag material = trim instanceof NbtTagCompound ? ((NbtTagCompound) trim).get("material") : null;
                        return material instanceof NbtTagString ? key(material.asString()) : null;
                    }
                    case "minecraft:block_state": {
                        NbtTag states = component(data, "minecraft:block_state");
                        String state = json.has("block_state_property") ? json.get("block_state_property").getAsString() : null;
                        NbtTag value = states instanceof NbtTagCompound && state != null ? ((NbtTagCompound) states).get(state) : null;
                        return value == null ? null : value.asString();
                    }
                    case "minecraft:component": {
                        NbtTag value = json.has("component") ? component(data, key(json.get("component").getAsString())) : null;
                        return value instanceof NbtTagString || numeric(value) ? value.asString() : null;
                    }
                    default:
                        return null;
                }
            }
            void setup(String name, NbtTagCompound data, String context, Model target) {
                String value = value(data, context);
                Node node = value != null && cases.containsKey(value) ? cases.get(value) : fallback;
                if (node != null) node.setup(name, data, context, target);
            }
        }

        static final class RangeDispatch extends Node {
            final String property;
            final JsonObject json;
            final float scale;
            final float[] thresholds;
            final Node[] models;
            final Node fallback;
            RangeDispatch(JsonObject json) {
                this.json = json;
                property = json.has("property") ? key(json.get("property").getAsString()) : "";
                scale = json.has("scale") ? json.get("scale").getAsFloat() : 1f;
                List<Object[]> entries = new ArrayList<>();
                if (json.get("entries") instanceof JsonArray)
                    for (JsonElement row : json.getAsJsonArray("entries")) {
                        if (!row.isJsonObject() || !row.getAsJsonObject().has("threshold")) continue;
                        entries.add(new Object[] {row.getAsJsonObject().get("threshold").getAsFloat(), parse(row.getAsJsonObject().getAsJsonObject("model"))});
                    }
                // Minecraft sorts the entries by threshold; packs often list them out of order.
                entries.sort(Comparator.comparingDouble(e -> (Float) e[0]));
                thresholds = new float[entries.size()];
                models = new Node[entries.size()];
                for (int i = 0; i < entries.size(); i++) { thresholds[i] = (Float) entries.get(i)[0]; models[i] = (Node) entries.get(i)[1]; }
                fallback = parse(json.getAsJsonObject("fallback"));
            }
            float value(NbtTagCompound data) {
                switch (property) {
                    case "minecraft:custom_model_data": {
                        int index = json.has("index") ? json.get("index").getAsInt() : 0;
                        List<NbtTag> floats = customModelData(data, "floats");
                        return index >= 0 && index < floats.size() ? floats.get(index).asFloat() : 0f;
                    }
                    case "minecraft:count": {
                        float count = intOf(data == null ? null : data.get("count"), intOf(data == null ? null : data.get("Count"), 1));
                        boolean normalize = !json.has("normalize") || json.get("normalize").getAsBoolean();
                        int max = intOf(component(data, "minecraft:max_stack_size"), 64);
                        return normalize ? Math.max(0f, Math.min(1f, count / Math.max(1, max))) : count;
                    }
                    case "minecraft:damage": {
                        int damage = intOf(component(data, "minecraft:damage"), intOf(legacyTag(data, "Damage"), 0));
                        int max = intOf(component(data, "minecraft:max_damage"), 0);
                        boolean normalize = !json.has("normalize") || json.get("normalize").getAsBoolean();
                        if (!normalize) return damage;
                        return max > 0 ? Math.max(0f, Math.min(1f, damage / (float) max)) : 0f;
                    }
                    case "minecraft:bundle/fullness": {
                        float total = 0f;
                        for (NbtTag stack : list(component(data, "minecraft:bundle_contents")))
                            if (stack instanceof NbtTagCompound) total += intOf(((NbtTagCompound) stack).get("count"), 1);
                        return Math.min(1f, total / 64f);
                    }
                    default:
                        // cooldown, use_duration, crossbow/pull, time, compass ...: a still item reads 0.
                        return 0f;
                }
            }
            void setup(String name, NbtTagCompound data, String context, Model target) {
                float value = value(data) * scale;
                Node node = fallback;
                if (!Float.isNaN(value)) {
                    int index = lastIndexLessOrEqual(thresholds, value);
                    if (index >= 0) node = models[index];
                }
                if (node != null) node.setup(name, data, context, target);
            }
            static int lastIndexLessOrEqual(float[] thresholds, float value) {
                int low = 0, high = thresholds.length - 1, found = -1;
                while (low <= high) {
                    int middle = (low + high) >>> 1;
                    if (thresholds[middle] <= value) { found = middle; low = middle + 1; }
                    else high = middle - 1;
                }
                return found;
            }
        }

        // --- Tint sources ----------------------------------------------------
        abstract static class Tint {
            final Integer fallback;
            Tint(JsonObject json) { fallback = Items.rgb(json.get("default")); }
            abstract Integer rgb(NbtTagCompound data);

            static Tint parse(JsonObject json) {
                String type = json.has("type") ? key(json.get("type").getAsString()) : "";
                switch (type) {
                    case "minecraft:constant": return new Tint(json) {
                        final Integer value = Items.rgb(json.get("value"));
                        Integer rgb(NbtTagCompound data) { return value; }
                    };
                    case "minecraft:dye": return new Tint(json) {
                        Integer rgb(NbtTagCompound data) {
                            NbtTag dyed = component(data, "minecraft:dyed_color");
                            if (dyed instanceof NbtTagCompound) dyed = ((NbtTagCompound) dyed).get("rgb");
                            if (dyed == null) {
                                NbtTag display = legacyTag(data, "display");
                                dyed = display instanceof NbtTagCompound ? ((NbtTagCompound) display).get("color") : null;
                            }
                            Integer value = Items.rgb(dyed);
                            return value != null ? value : fallback;
                        }
                    };
                    case "minecraft:firework": return new Tint(json) {
                        Integer rgb(NbtTagCompound data) {
                            NbtTag explosion = component(data, "minecraft:firework_explosion");
                            List<NbtTag> colours = list(explosion instanceof NbtTagCompound ? ((NbtTagCompound) explosion).get("colors") : null);
                            if (colours.isEmpty()) return fallback;
                            if (colours.size() == 1) return colours.get(0).asInt() & 0xFFFFFF;
                            int r = 0, g = 0, b = 0;
                            for (NbtTag colour : colours) {
                                int value = colour.asInt();
                                r += value >> 16 & 255; g += value >> 8 & 255; b += value & 255;
                            }
                            int n = colours.size();
                            return (r / n) << 16 | (g / n) << 8 | (b / n);
                        }
                    };
                    case "minecraft:map_color": return new Tint(json) {
                        Integer rgb(NbtTagCompound data) {
                            Integer value = Items.rgb(component(data, "minecraft:map_color"));
                            return value != null ? value : fallback;
                        }
                    };
                    case "minecraft:potion": return new Tint(json) {
                        Integer rgb(NbtTagCompound data) {
                            NbtTag contents = component(data, "minecraft:potion_contents");
                            Integer value = contents instanceof NbtTagCompound ? Items.rgb(((NbtTagCompound) contents).get("custom_color")) : null;
                            return value != null ? value : fallback;
                        }
                    };
                    case "minecraft:custom_model_data": return new Tint(json) {
                        final int index = json.has("index") ? json.get("index").getAsInt() : 0;
                        Integer rgb(NbtTagCompound data) {
                            List<NbtTag> colours = customModelData(data, "colors");
                            Integer value = index >= 0 && index < colours.size() ? Items.rgb(colours.get(index)) : null;
                            return value != null ? value : fallback;
                        }
                    };
                    case "minecraft:grass": return new Tint(json) {
                        final float temperature = json.has("temperature") ? json.get("temperature").getAsFloat() : .5f;
                        final float downfall = json.has("downfall") ? json.get("downfall").getAsFloat() : .5f;
                        Integer rgb(NbtTagCompound data) {
                            BufferedImage map = Tints.getColorMap("minecraft:grass");
                            if (map == null) return null;
                            float t = Math.max(0f, Math.min(1f, temperature));
                            float d = Math.max(0f, Math.min(1f, downfall)) * t;
                            int x = (int) ((1f - t) * (map.getWidth() - 1)), y = (int) ((1f - d) * (map.getHeight() - 1));
                            return map.getRGB(x, y) & 0xFFFFFF;
                        }
                    };
                    default: return new Tint(json) {
                        // team and anything newer: its default colour.
                        Integer rgb(NbtTagCompound data) { return fallback; }
                    };
                }
            }
        }

        static final class Handler extends ItemHandler {
            final Node root;
            Handler(Node root) { this.root = root; }
            public Model getModel(String name, NbtTagCompound data, String context) {
                Model model = new Model(name, null, false, false);
                if (data == null) data = NbtTagCompound.newNonPooledInstance("properties");
                root.setup(name, data, context, model);
                return model;
            }
        }

        // --- Display transforms ---------------------------------------------------
        // MiEx turns a model's "display" entry into translate(-x, y, -z) and
        // rotateZ(z)·rotateY(-y)·rotateX(-x), so any item with a display rotation or
        // translation (all of Origin Realms' furniture) sat mirrored or a block away.
        // Minecraft's ItemTransform: translate (clamped to 80/16), rotationXYZ, scale
        // (clamped to 4), about the model's centre; inherited per context from parents.
        static final class Display {
            private static final Map<String, Object> cache = new ConcurrentHashMap<>();
            private static volatile String packs = "";

            static Model apply(String model, Model base, String context) {
                Matrix matrix = matrix(model, context);
                if (matrix == null) return base;
                Model copy = new Model(base);
                copy.transform(matrix);
                return copy;
            }

            static Matrix matrix(String model, String context) {
                if (model == null || context == null || context.isEmpty() || context.equals("none")) return null;
                StringBuilder key = new StringBuilder();
                for (ResourcePack pack : ResourcePacks.getActiveResourcePacks())
                    key.append(pack.getUUID()).append('@').append(System.identityHashCode(pack)).append(';');
                if (!key.toString().equals(packs)) { cache.clear(); packs = key.toString(); }
                String name = key(model);
                Object hit = cache.computeIfAbsent(name + '|' + context, ignored -> find(name, context));
                return hit instanceof Matrix ? (Matrix) hit : null;
            }

            static Object find(String name, String context) {
                String current = name;
                for (int depth = 0; current != null && depth < 32; depth++) {
                    current = key(current);
                    if (current.startsWith("minecraft:builtin/")) break;
                    JsonObject json = ResourcePacks.getJSONData(current, "models", "assets");
                    if (json == null) break;
                    if (json.get("display") instanceof JsonObject && json.getAsJsonObject("display").get(context) instanceof JsonObject)
                        return build(json.getAsJsonObject("display").getAsJsonObject(context));
                    current = json.has("parent") && json.get("parent").isJsonPrimitive() ? json.get("parent").getAsString() : null;
                }
                return Boolean.FALSE;
            }

            static double[] vector(JsonObject json, String field, double fallback) {
                double[] out = {fallback, fallback, fallback};
                if (json.get(field) instanceof JsonArray) {
                    JsonArray values = json.getAsJsonArray(field);
                    for (int i = 0; i < 3 && i < values.size(); i++) out[i] = values.get(i).getAsDouble();
                }
                return out;
            }

            static double[][] multiply(double[][] a, double[][] b) {
                double[][] out = new double[4][4];
                for (int i = 0; i < 4; i++)
                    for (int j = 0; j < 4; j++)
                        for (int k = 0; k < 4; k++) out[i][j] += a[i][k] * b[k][j];
                return out;
            }

            static double[][] translation(double x, double y, double z) {
                return new double[][] {{1, 0, 0, x}, {0, 1, 0, y}, {0, 0, 1, z}, {0, 0, 0, 1}};
            }

            static double[][] rotation(int axis, double degrees) {
                double c = Math.cos(Math.toRadians(degrees)), s = Math.sin(Math.toRadians(degrees));
                if (axis == 0) return new double[][] {{1, 0, 0, 0}, {0, c, -s, 0}, {0, s, c, 0}, {0, 0, 0, 1}};
                if (axis == 1) return new double[][] {{c, 0, s, 0}, {0, 1, 0, 0}, {-s, 0, c, 0}, {0, 0, 0, 1}};
                return new double[][] {{c, -s, 0, 0}, {s, c, 0, 0}, {0, 0, 1, 0}, {0, 0, 0, 1}};
            }

            static Matrix build(JsonObject transform) {
                double[] t = vector(transform, "translation", 0), r = vector(transform, "rotation", 0), s = vector(transform, "scale", 1);
                for (int i = 0; i < 3; i++) { t[i] = Math.max(-80, Math.min(80, t[i])); s[i] = Math.max(-4, Math.min(4, s[i])); }
                double[][] m = translation(8 + t[0], 8 + t[1], 8 + t[2]);
                m = multiply(m, rotation(0, r[0]));
                m = multiply(m, rotation(1, r[1]));
                m = multiply(m, rotation(2, r[2]));
                m = multiply(m, new double[][] {{s[0], 0, 0, 0}, {0, s[1], 0, 0}, {0, 0, s[2], 0}, {0, 0, 0, 1}});
                m = multiply(m, translation(-8, -8, -8));
                float[] data = new float[16];
                for (int i = 0; i < 4; i++) for (int j = 0; j < 4; j++) data[i * 4 + j] = (float) m[i][j];
                return new Matrix(data);
            }
        }

        // --- Pre-1.21.4 "overrides" in models/item/*.json ---------------------
        // Minecraft tests every override and the last one whose predicates are all
        // at least their value wins; MiEx took the first exact match.
        static final class Legacy {
            final String model;
            final List<String> models = new ArrayList<>();
            final List<Map<String, Float>> predicates = new ArrayList<>();
            Legacy(String model, JsonArray overrides) {
                this.model = model;
                for (JsonElement row : overrides) {
                    if (!row.isJsonObject() || !row.getAsJsonObject().has("model")) continue;
                    Map<String, Float> values = new LinkedHashMap<>();
                    JsonObject predicate = row.getAsJsonObject().getAsJsonObject("predicate");
                    if (predicate != null)
                        for (Map.Entry<String, JsonElement> entry : predicate.entrySet())
                            values.put(key(entry.getKey()), entry.getValue().getAsFloat());
                    models.add(row.getAsJsonObject().get("model").getAsString());
                    predicates.add(values);
                }
            }
            static float property(String name, NbtTagCompound data) {
                switch (name) {
                    case "minecraft:custom_model_data": {
                        List<NbtTag> floats = customModelData(data, "floats");
                        return floats.isEmpty() ? 0f : (float) floats.get(0).asInt();
                    }
                    case "minecraft:damaged":
                        return intOf(component(data, "minecraft:damage"), intOf(legacyTag(data, "Damage"), 0)) > 0 ? 1f : 0f;
                    case "minecraft:damage": {
                        int max = intOf(component(data, "minecraft:max_damage"), 0);
                        int damage = intOf(component(data, "minecraft:damage"), intOf(legacyTag(data, "Damage"), 0));
                        return max > 0 ? Math.max(0f, Math.min(1f, damage / (float) max)) : 0f;
                    }
                    case "minecraft:charged":
                        return list(component(data, "minecraft:charged_projectiles")).isEmpty() ? 0f : 1f;
                    case "minecraft:lefthanded": case "minecraft:cooldown": case "minecraft:pull": case "minecraft:pulling":
                    case "minecraft:blocking": case "minecraft:throwing": case "minecraft:cast": case "minecraft:tooting":
                    case "minecraft:brushing": case "minecraft:time": case "minecraft:angle": case "minecraft:firework":
                    case "minecraft:filled": case "minecraft:broken":
                        return 0f;
                    default:
                        // Minecraft: an unregistered property never matches.
                        return Float.NEGATIVE_INFINITY;
                }
            }
            String resolve(NbtTagCompound data) {
                String chosen = model;
                for (int i = 0; i < models.size(); i++) {
                    boolean match = true;
                    for (Map.Entry<String, Float> predicate : predicates.get(i).entrySet())
                        if (property(predicate.getKey(), data) < predicate.getValue()) { match = false; break; }
                    if (match) chosen = models.get(i);
                }
                return chosen;
            }
        }

        static final class LegacyHandler extends ItemHandler {
            final Legacy legacy;
            LegacyHandler(Legacy legacy) { this.legacy = legacy; }
            public Model getModel(String name, NbtTagCompound data, String context) {
                if (data == null) data = NbtTagCompound.newNonPooledInstance("properties");
                String chosen = legacy.resolve(data);
                Model base = ModelRegistry.getModel(ModelRegistry.getIdForName(chosen, false));
                Model model = new Model(name, null, false, false);
                if (base == null) return model;
                // Vanilla's item colours for the layers they tint (ItemColors before 1.21.4).
                List<Color> tints = new ArrayList<>();
                NbtTag dyed = component(data, "minecraft:dyed_color");
                if (dyed instanceof NbtTagCompound) dyed = ((NbtTagCompound) dyed).get("rgb");
                if (dyed == null) {
                    NbtTag display = legacyTag(data, "display");
                    dyed = display instanceof NbtTagCompound ? ((NbtTagCompound) display).get("color") : null;
                }
                Integer dye = rgb(dyed);
                tints.add(dye == null ? new Color(1f, 1f, 1f) : new Color(dye));
                String item = key(name);
                if (item.equals("minecraft:firework_star")) {
                    NbtTag explosion = component(data, "minecraft:firework_explosion");
                    List<NbtTag> colours = list(explosion instanceof NbtTagCompound ? ((NbtTagCompound) explosion).get("colors") : null);
                    int r = 0, g = 0, b = 0;
                    for (NbtTag colour : colours) { int v = colour.asInt(); r += v >> 16 & 255; g += v >> 8 & 255; b += v & 255; }
                    int n = colours.size();
                    tints.add(n == 0 ? new Color(0x8A8A8A) : new Color((r / n) << 16 | (g / n) << 8 | (b / n)));
                } else if (item.equals("minecraft:filled_map")) {
                    Integer colour = rgb(component(data, "minecraft:map_color"));
                    tints.add(new Color(colour == null ? 0x46402E : colour));
                }
                base = Display.apply(chosen, base, context);
                model.addModel(base, tints);
                return model;
            }
        }
    }
}
