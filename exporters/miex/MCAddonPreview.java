// Sparkle's 3D area preview for MiEx experimental-28.
// MCAddonPreviewer is MiEx's ChunkPreviewer (nl.bramstout.mcworldexporter.preview,
// Copyright Bram Stout Productions, BSD 3-Clause; see LICENSE-MiEx.txt) with these
// changes: blocks outside the previewed area count as air, so its cut sides show,
// as in Mine-imator's world import; world offsets default to zero; tints are
// copied, never multiplied in place (upstream darkens the shared biome colours);
// detailed occlusion gets an empty face list instead of null.
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import nl.bramstout.mcworldexporter.commands.Command;
import nl.bramstout.mcworldexporter.parallel.ThreadPool.Task;
import nl.bramstout.mcworldexporter.ui.Renderer2D;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import nl.bramstout.mcworldexporter.Color;
import nl.bramstout.mcworldexporter.Config;
import nl.bramstout.mcworldexporter.MCWorldExporter;
import nl.bramstout.mcworldexporter.atlas.Atlas;
import nl.bramstout.mcworldexporter.export.BlendedBiome;
import nl.bramstout.mcworldexporter.export.BlendedBiome.WeightedColor;
import nl.bramstout.mcworldexporter.export.ChunkExporter;
import nl.bramstout.mcworldexporter.export.ChunkExporter.MeshKey;
import nl.bramstout.mcworldexporter.export.Mesh;
import nl.bramstout.mcworldexporter.export.MeshPurpose;
import nl.bramstout.mcworldexporter.export.VertexColorSet;
import nl.bramstout.mcworldexporter.model.BakedBlockState;
import nl.bramstout.mcworldexporter.model.BlockStateRegistry;
import nl.bramstout.mcworldexporter.model.Direction;
import nl.bramstout.mcworldexporter.model.Model;
import nl.bramstout.mcworldexporter.model.ModelFace;
import nl.bramstout.mcworldexporter.model.Occlusion;
import nl.bramstout.mcworldexporter.resourcepack.Biome;
import nl.bramstout.mcworldexporter.resourcepack.MCMeta;
import nl.bramstout.mcworldexporter.resourcepack.ResourcePacks;
import nl.bramstout.mcworldexporter.resourcepack.Tints.TintLayers;
import nl.bramstout.mcworldexporter.resourcepack.Tints.TintValue;
import nl.bramstout.mcworldexporter.world.BiomeRegistry;
import nl.bramstout.mcworldexporter.world.Chunk;
import nl.bramstout.mcworldexporter.world.LayeredBlock;


public class MCAddonPreview extends Command {
	private static int read(JsonObject row, String key, int fallback) {
		return row.has(key) ? row.get(key).getAsInt() : fallback;
	}

	@Override
	public JsonObject run(JsonObject command) {
		JsonArray chunks = new JsonArray();
		List<Task> tasks = new ArrayList<Task>();
		if(command.has("chunks") && MCWorldExporter.getApp().getWorld() != null) {
			for(JsonElement element : command.getAsJsonArray("chunks")) {
				final JsonObject row = element.getAsJsonObject();
				tasks.add(Renderer2D.threadPool.submit(() -> {
					try {
						Chunk chunk = MCWorldExporter.getApp().getWorld().getChunk(read(row, "chunkX", 0), read(row, "chunkZ", 0));
						if(chunk == null)
							return;
						MCAddonPreviewer previewer = new MCAddonPreviewer(chunk);
						previewer.minX = read(row, "minX", previewer.minX);
						previewer.minY = read(row, "minY", previewer.minY);
						previewer.minZ = read(row, "minZ", previewer.minZ);
						previewer.maxX = read(row, "maxX", previewer.maxX);
						previewer.maxY = read(row, "maxY", previewer.maxY);
						previewer.maxZ = read(row, "maxZ", previewer.maxZ);
						previewer.lodSize = read(row, "lodSize", 1);
						previewer.lodYSize = read(row, "lodYSize", 1);
						previewer.worldOffsetX = read(row, "worldOffsetX", 0);
						previewer.worldOffsetY = read(row, "worldOffsetY", 0);
						previewer.worldOffsetZ = read(row, "worldOffsetZ", 0);
						previewer.areaMinX = read(row, "areaMinX", Integer.MIN_VALUE);
						previewer.areaMinY = read(row, "areaMinY", Integer.MIN_VALUE);
						previewer.areaMinZ = read(row, "areaMinZ", Integer.MIN_VALUE);
						previewer.areaMaxX = read(row, "areaMaxX", Integer.MAX_VALUE);
						previewer.areaMaxY = read(row, "areaMaxY", Integer.MAX_VALUE);
						previewer.areaMaxZ = read(row, "areaMaxZ", Integer.MAX_VALUE);
						if(row.has("originOnBlockCenter"))
							previewer.originOnBlockCenter = row.get("originOnBlockCenter").getAsBoolean();
						if(row.has("singleMeshNoTextures"))
							previewer.singleMeshNoTextures = row.get("singleMeshNoTextures").getAsBoolean();
						previewer.generatePreviewMeshes();
						JsonObject result = new JsonObject();
						result.addProperty("chunkX", chunk.getChunkX());
						result.addProperty("chunkZ", chunk.getChunkZ());
						JsonArray meshes = new JsonArray();
						for(Mesh mesh : previewer.getMeshes())
							meshes.add(mesh.toJson());
						result.add("meshes", meshes);
						synchronized(chunks) {
							chunks.add(result);
						}
					}catch(Exception ex) {
						ex.printStackTrace();
					}
				}));
			}
		}
		for(Task task : tasks)
			task.waitUntilTaskIsDoneFast();
		JsonObject result = new JsonObject();
		result.add("chunks", chunks);
		return result;
	}
}

class MCAddonPreviewer {
	
	private Chunk chunk;
	private int[] lodBlockIds = new int[ChunkExporter.lodBlockIdsSize];
	private int OCCLUSION_BLOCK_ID[] = new int[4];
	private LayeredBlock layeredBlockCache = new LayeredBlock();
	private Occlusion occlusionHandler = new Occlusion();
	private Map<MeshKey, Mesh> meshes = new java.util.HashMap<MeshKey, Mesh>();
	private static final List<ModelFace> NO_DETAILED_FACES = java.util.Collections.emptyList();
	private Mesh singleMesh;
	private MeshKey meshKeyCache = new MeshKey();
	private Color[] faceTint = null;
	
	public int minX = 0;
	public int minY = -64;
	public int minZ = 0;
	public int maxX = 16;
	public int maxY = 320;
	public int maxZ = 16;
	public int lodSize = 1;
	public int lodYSize = 1;
	public int worldOffsetX = 0;
	public int worldOffsetY = 0;
	public int worldOffsetZ = 0;
	public boolean originOnBlockCenter = false;
	public boolean singleMeshNoTextures = true;
	// Sparkle: the previewed area in world blocks, maximums exclusive. Blocks
	// outside it count as air, so the area's cut sides are drawn.
	public int areaMinX = Integer.MIN_VALUE;
	public int areaMinY = Integer.MIN_VALUE;
	public int areaMinZ = Integer.MIN_VALUE;
	public int areaMaxX = Integer.MAX_VALUE;
	public int areaMaxY = Integer.MAX_VALUE;
	public int areaMaxZ = Integer.MAX_VALUE;
	
	public MCAddonPreviewer(Chunk chunk) {
		this.chunk = chunk;
	}
	
	public List<Mesh> getMeshes(){
		if(singleMeshNoTextures) {
			List<Mesh> res = new ArrayList<Mesh>();
			if(singleMesh != null)
				res.add(singleMesh);
			return res;
		}else {
			return new ArrayList<Mesh>(meshes.values());
		}
	}
	
	public void generatePreviewMeshes() {
		int bx = 0;
		int by = 0;
		int bz = 0;
		int wx = 0;
		int wz = 0;
		int chunkWorldX = chunk.getChunkX() * 16;
		int chunkWorldZ = chunk.getChunkZ() * 16;
		int blockId[] = new int[4];
		BakedBlockState state = null;
		long occlusion = 0;
		BlendedBiome biome = new BlendedBiome();
		List<Model> models = new ArrayList<Model>();
		
		
		for(by = minY; by < maxY; by += lodYSize) {
			for(bz = minZ; bz < maxZ; bz += lodSize) {
				wz = bz + chunkWorldZ;
				for(bx = minX; bx < maxX; bx += lodSize) {
					wx = bx + chunkWorldX;
					
					boolean calculatedBiome = false;
					for(int layer = 0; layer < chunk.getLayerCount(); ++layer) {	
						ChunkExporter.getLODBlockId(chunk, bx, by, bz, layer, lodSize, lodYSize, lodBlockIds, null, blockId);
						if(blockId[0] < 0)
							continue;
						state = BlockStateRegistry.getBakedStateForBlock(blockId[0], blockId[1], blockId[2], blockId[3], layer, false);
						
						occlusion = getOcclusion(chunk, bx, by, bz, state, lodSize, lodYSize);
						if(occlusion == Long.MAX_VALUE) {
							continue; // Block is in cave
						}
						
						int biomeId = chunk.getBiomeIdLocal(bx, by, bz);
						Biome biomeInstance = BiomeRegistry.getBiome(biomeId);
						biome.clear();
						
						if(state.getTint() != null && !calculatedBiome) {
							getBlendedBiome(chunk, wx, by, wz, biome);
							calculatedBiome = true;
						}
						
						handleBlock(models, state, blockId, chunk.getDataVersion(), occlusion, 
									wx, by, wz, layer, biomeInstance, biome, 
									lodSize, lodYSize, occlusionHandler, chunk, worldOffsetX, worldOffsetY, worldOffsetZ);
					}
				}
			}
		}
	}
	
	private void handleBlock(List<Model> models, BakedBlockState state, int[] blockId, int dataVersion, long occlusion, 
							int wx, int by, int wz, int layer, Biome biomeInstance, BlendedBiome biome, 
							int lodSize, int lodYSize, Occlusion occlusionHandler, Chunk chunk, 
							int worldOffsetX, int worldOffsetY, int worldOffsetZ) {
		models.clear();
		state.getModels(blockId[1], blockId[2], blockId[3], models, layeredBlockCache);
		
		// Sparkle: an empty list, not null. Blocks with detailed occlusion (leaves
		// among them) read it, and null threw away the whole chunk.
		occlusionHandler.calculateOcclusionDataForModel(models, state, occlusion, NO_DETAILED_FACES);
		
		byte blockLightEmission = state.getEmissiveLightLevel();
		
		Model model;
		ModelFace face;
		int faceIndex = 0;
		for(int i = 0; i < models.size(); ++i) {
			model = models.get(i);
			for(int j = 0; j < model.getFaces().size(); ++j) {
				if(occlusionHandler.isFaceOccluded(faceIndex)) {
					faceIndex++;
					continue;
				}
				
				face = model.getFaces().get(j);
				
				addFace(meshes, state.getName(), blockId[0], dataVersion, face, model.getTexture(face.getTexture()), 
						biomeInstance, biome, wx, by, wz, layer, 
						model.getExtraData(), state.getTint(), model.isDoubleSided(), 
						lodSize, lodYSize, state.isLodNoUVScale(), state.isLodNoScale(), false, state.getSeparateMeshForBlock(), 
						blockLightEmission, chunk, worldOffsetX, worldOffsetY, worldOffsetZ);
				
				faceIndex++;
			}
		}
	}
	
	
	// Sparkle: upstream returns 0 (black) and caches it when the winning pack's
	// texture is missing or unreadable, so those blocks vanish into the dark
	// background. Try the texture in every active pack and the base pack instead.
	private static final int NEUTRAL = 0xFF808080;
	private static final Map<String, Integer> fallbackColours = new java.util.concurrent.ConcurrentHashMap<String, Integer>();
	private static volatile List<?> fallbackPacks = null;

	static int colourOf(String texture) {
		int colour = 0;
		// Upstream throws on its first read of an unreadable image, which dropped that chunk.
		try { colour = ResourcePacks.getDefaultColour(texture); } catch(Exception ex) {}
		if(colour != 0)
			return colour;
		List<nl.bramstout.mcworldexporter.resourcepack.ResourcePack> packs = ResourcePacks.getActiveResourcePacks();
		if(fallbackPacks != packs) {
			fallbackColours.clear();
			fallbackPacks = packs;
		}
		return fallbackColours.computeIfAbsent(texture, key -> {
			List<nl.bramstout.mcworldexporter.resourcepack.ResourcePack> candidates = new ArrayList<>(packs);
			candidates.add(ResourcePacks.getBaseResourcePack());
			for(nl.bramstout.mcworldexporter.resourcepack.ResourcePack pack : candidates) {
				try {
					java.io.File file = pack == null ? null : pack.getTexture(key);
					java.awt.image.BufferedImage image = file != null && file.isFile() ? nl.bramstout.mcworldexporter.image.ImageReader.readImage(file) : null;
					int average = image == null ? 0 : average(image);
					if(average != 0)
						return average;
				}catch(Exception ex) {}
			}
			System.out.println("3D preview has no readable texture for " + key + "; drawing it grey.");
			return NEUTRAL;
		});
	}

	private static int average(java.awt.image.BufferedImage image) {
		long r = 0, g = 0, b = 0, count = 0;
		for(int y = 0; y < image.getHeight(); ++y) {
			for(int x = 0; x < image.getWidth(); ++x) {
				int argb = image.getRGB(x, y);
				if((argb >>> 24) == 0)
					continue;
				r += (argb >>> 16) & 0xFF; g += (argb >>> 8) & 0xFF; b += argb & 0xFF; ++count;
			}
		}
		return count == 0 ? 0 : 0xFF000000 | (int) (r / count) << 16 | (int) (g / count) << 8 | (int) (b / count);
	}

	private void addFace(Map<MeshKey, Mesh> meshes, String blockName, int blockId, int dataVersion, ModelFace face, String texture, 
			Biome biome, BlendedBiome blendedBiome, int ix, int iy, int iz, int layer, String extraData, 
			TintLayers tintLayers, boolean doubleSided, int lodSize, int lodYSize,
			boolean lodNoUVScale, boolean lodNoScale, boolean noConnectedTextures, boolean separateMeshForBlock, 
			byte blockLightEmission, Chunk chunk, int worldOffsetX, int worldOffsetY, int worldOffsetZ) {
		if(texture == null || texture.equals("") || Config.bannedMaterials.contains(texture))
			return;
		
		float[] normal = null;
		VertexColorSet.VertexColorFace[] vertexColors = null;
		
		
		
		String matTexture = texture;
		meshKeyCache.materialTemplate = null;
		meshKeyCache.texture = texture;
		meshKeyCache.blockName = null;
		meshKeyCache.atlasItem = null;
		meshKeyCache.hasTint = false;
		meshKeyCache.shadingMode = null;
		meshKeyCache.blockLightEmission = blockLightEmission;
		meshKeyCache.doubleSided = face.isDoubleSided();
		if(faceTint == null || faceTint.length != 1) {
			faceTint = new Color[1];
		}
		Color[] tint = null;
		if(tintLayers != null) {
			int tintIndex = face.getTintIndex();
			if(tintIndex < 0 && Config.forceBiomeColor.contains(texture))
				tintIndex = 0;
			TintValue tintValue = tintLayers.getLayer(tintIndex);
			if(tintValue != null) {
				tint = faceTint;
				WeightedColor color = tintValue.getColor(blendedBiome);
				if(color == null) {
					tint = null;
				}else {
					for(int i = 0; i < tint.length; ++i) {
						tint[i] = color.get(i);
						if(tint[i] == null) {
							tint = null;
							break;
						}
					}
				}
			}
		}
		if(tint != null) {
			// If the face doesn't have a tintIndex, get rid of the tint.
			// This is also how Minecraft does it.
			// But don't do it, if we want to force the biome colour anyways.
			if((face.getTintIndex() < 0 && !Config.forceBiomeColor.contains(texture)) || 
					Config.forceNoBiomeColor.contains(blockName))
				tint = null;
			else
				meshKeyCache.hasTint = true;
		}
		
		if(singleMeshNoTextures) {
			int colour = colourOf(texture);
			int r = (colour >>> 16) & 0xFF;
			int g = (colour >>> 8) & 0xFF;
			int b = (colour) & 0xFF;
			float fr = (float) Math.pow(((float) r) / 255f, 2.2f);
			float fg = (float) Math.pow(((float) g) / 255f, 2.2f);
			float fb = (float) Math.pow(((float) b) / 255f, 2.2f);
			
			// Sparkle: a new Color. Upstream multiplies in place, but the tint is
			// the biome's own shared Color, so every face darkened grass and
			// foliage for the map, later previews and exports until black.
			if(tint == null) {
				tint = faceTint;
				tint[0] = new Color(fr, fg, fb);
			}else {
				tint[0] = new Color(tint[0].r * fr, tint[0].g * fg, tint[0].b * fb);
			}
		}
		
		if(!face.getShadingMode().equals(ModelFace.SHADING_MODE_STANDARD)) {
			meshKeyCache.shadingMode = face.getShadingMode();
		}
		float lodSizeF = ((float) ((lodNoScale ? 1 : lodSize)-1)) / 2.0f;
		float lodYSizeF = ((float) (lodYSize-1)) / 2.0f;
		float lodScale = (float) (lodNoScale ? 1 : lodSize);
		float lodYScale = (float) (lodNoScale ? 1 : lodYSize);
		float lodUVScale = lodNoUVScale ? 1.0f : lodScale;
		float lodYUVScale = lodNoUVScale ? 1.0f : lodYScale;
		Atlas.AtlasItem atlas = null;
		if(!singleMeshNoTextures) {
			atlas = Atlas.getAtlasItem(texture);
			if(atlas != null) {
				meshKeyCache.atlasItem = atlas;
				texture = atlas.atlas;
				// When using an atlas, we can't just scale up the UVs.
				lodUVScale = Math.min(lodUVScale, (float) atlas.padding);
				lodYUVScale = Math.min(lodYUVScale, (float) atlas.padding);
			}
		}
		// Scale the Y uv's on the top and bottom faces like normal.
		if(face.getDirection() == Direction.UP || face.getDirection() == Direction.DOWN)
			lodYUVScale = lodUVScale;
				
		if(separateMeshForBlock) {
			meshKeyCache.blockName = blockName;
		}
		
		Mesh mesh = null;
		if(singleMeshNoTextures) {
			if(singleMesh == null) {
				singleMesh = new Mesh("ChunkMesh", MeshPurpose.UNDEFINED, "vertexcolor:vertexcolor", "vertexcolor:vertexcolor", 
						false, false, ModelFace.SHADING_MODE_STANDARD, 1024, 8);
			}
			mesh = singleMesh;
		}else {
			mesh = meshes.getOrDefault(meshKeyCache, null);
			if(mesh == null) {
				boolean animatedTexture = false;
				MCMeta mcmeta = ResourcePacks.getMCMeta(texture);
				if(mcmeta != null)
					animatedTexture = mcmeta.isAnimate() || mcmeta.isInterpolate();
				
				mesh = new Mesh(meshKeyCache.createName(), MeshPurpose.UNDEFINED, texture, matTexture, animatedTexture, doubleSided,
								face.getShadingMode(), 1024, 8);
				mesh.setExtraData(extraData);
				mesh.setBlockLightEmission(blockLightEmission);
				meshes.put(meshKeyCache, mesh);
				meshKeyCache = new MeshKey();
			}
		}
		
		float blockSubOffset = originOnBlockCenter ? 0.5f : 0f;
		
		mesh.addFace(face, 
				ix - worldOffsetX - blockSubOffset + lodSizeF, iy - worldOffsetY + lodYSizeF, iz - worldOffsetZ - blockSubOffset + lodSizeF, 
				0, 0, 0, 0, lodScale, lodYScale, lodUVScale, lodYUVScale, atlas, 
				tint, null, 0, vertexColors, normal);
	}
	

	
	private void getBlendedBiome(Chunk chunk, int wx, int wy, int wz, BlendedBiome res) {
		int biomeId;
		Biome biome;
		
		if(chunk != null) {
			biomeId = chunk.getBiomeIdLocal(wx & 15, wy, wz & 15);
			biome = BiomeRegistry.getBiome(biomeId);
			res.addBiome(biome, 1f, 1f, 1f, 1f, 1f, 1f, 1f, 1f);
		}
		
		if(chunk != null) {
			chunk.addBiomeTints(res, wx & 15, wy, wz & 15, 0);
		}
		
		res.normalise();
	}
	
	private long getOcclusion(Chunk chunk, int cx, int cy, int cz, BakedBlockState currentState, 
								int lodSize, int lodYSize) {
		long occlusion = 0;
		
		long occludes = 0;
		for(Direction dir : Direction.CACHED_VALUES) {
		int bx = cx + dir.x * lodSize;
		int by = cy + dir.y * lodYSize;
		int bz = cz + dir.z * lodSize;
		int nx = bx + chunk.getChunkX() * 16;
		int nz = bz + chunk.getChunkZ() * 16;
		if(nx < areaMinX || nx >= areaMaxX || by < areaMinY || by >= areaMaxY || nz < areaMinZ || nz >= areaMaxZ)
			continue; // Sparkle: outside the area is air.
		
		for(int layer = 0; layer < chunk.getLayerCount(); ++layer) {
			occludes = getOcclusionFromDirection(chunk, bx, by, bz, layer, dir, currentState, lodSize, lodYSize);
			if((occludes & (0b1L << 32)) != 0)
				// Safe out that we have another leaf here.
				// Used by the corner algorithm.
				occlusion |= 0b1 << (dir.id + 32);
				
				occludes &= 0b1111;
				occludes <<= dir.id * 4;
				occlusion |= occludes;
			}
		}
				
		return occlusion;
	}
	
	private long getOcclusionFromDirection(int wx, int wy, int wz, int layer, Direction dir, BakedBlockState currentState,
											int lodSize, int lodYSize) {
		try {
			Chunk chunk = MCWorldExporter.getApp().getWorld().getChunk(wx >> 4, wz >> 4);
			if (chunk != null && !chunk.hasLoadError()) {
				return getOcclusionFromDirection(chunk, wx - chunk.getChunkX() * 16, wy, wz - chunk.getChunkZ() * 16, layer,
				dir, currentState, lodSize, lodYSize);
			}
		}catch(Exception ex) {
			ex.printStackTrace();
		}
		// No chunk, so return that it's occluded. This gets rid of the side of the world.
		return 0b1111L;
	}
	
	private long getOcclusionFromDirection(Chunk chunk, int bx, int by, int bz, int layer, Direction dir, BakedBlockState currentState,
			int lodSize, int lodYSize) {
		if(bx < 0 || bx >= 16 || bz < 0 || bz >= 16) {
			return getOcclusionFromDirection(bx + chunk.getChunkX() * 16, by, bz + chunk.getChunkZ() * 16, layer, dir, 
					currentState, lodSize, lodYSize);
		}
		
		int sampleLodSize = lodSize;
		int sampleLodYSize = lodYSize;
		int sampleLodLevel = Integer.numberOfTrailingZeros(sampleLodSize);
		int sampleLodYLevel = Integer.numberOfTrailingZeros(sampleLodYSize);
		
		ChunkExporter.getLODBlockId(chunk, 
			(bx >> sampleLodLevel) << sampleLodLevel, 
			(by >> sampleLodYLevel) << sampleLodYLevel, 
			(bz >> sampleLodLevel) << sampleLodLevel, 
			layer,
			sampleLodSize, sampleLodYSize, lodBlockIds, null, OCCLUSION_BLOCK_ID);
		
		if(OCCLUSION_BLOCK_ID[0] < 0) {
			// If the block id is less than 0, that means
			// that there was no chunk, so let's say that it does
			// occlude. This gets rid of the side of the world.
			return 0b1111L;
		}
	
		BakedBlockState state = BlockStateRegistry.getBakedStateForBlock(OCCLUSION_BLOCK_ID[0], OCCLUSION_BLOCK_ID[1],
		OCCLUSION_BLOCK_ID[2], OCCLUSION_BLOCK_ID[3], layer, false);
		
		// Transparent blocks don't occlude non-transparent blocks or liquid blocks
		if(state.isTransparentOcclusion() && (!currentState.isTransparentOcclusion() || currentState.hasLiquid()))
			return 0;
		// Leaves blocks don't occlude non-leaves and non-transparent blocks
		if(state.isLeavesOcclusion() && !currentState.isLeavesOcclusion() && !currentState.isTransparentOcclusion())
			return 0;
		// If both sides are leaves blocks, then only one side kept.
		if(state.isLeavesOcclusion() && currentState.isLeavesOcclusion()) {
			// Safe out that we have another leaf here.
			// Used by the corner algorithm.
			if(!state.isDoubleSided())
				return 0b1L << 32;
			else if(dir == Direction.DOWN || dir == Direction.SOUTH || dir == Direction.WEST)
				return 0b1L << 32;
		}
	
		
	
		long occludes = state.getOccludes();
		occludes >>= dir.getOpposite().id * 4;
		occludes &= 0b1111L;
		
		return occludes;
	}
	
}
