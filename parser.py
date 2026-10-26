from .importers.bedrock_assets import load_json_file
from .particle_core import SourceRef, normalize_particle_document


class BedrockParticleParser:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = {}
        self.components = {}
        self.curves = {}
        self.materials = {}
        self.textures = {}
        
        self.data = load_json_file(filepath)

        self.particle_effect = self.data.get("particle_effect", self.data.get("minecraft:particle_effect", {}))
        
        if not self.particle_effect:
            if "description" in self.data and "components" in self.data:
                self.particle_effect = self.data
            else:
                for key, value in self.data.items():
                    if isinstance(value, dict) and "description" in value and "components" in value:
                        self.particle_effect = value
                        break

        self.description = self.particle_effect.get("description", {})
        self.components = self.particle_effect.get("components", {})
        self.events = self.particle_effect.get("events", {})
        canonical = {
            "format_version": self.data.get("format_version", ""),
            "particle_effect": self.particle_effect,
        }
        self.normalization = normalize_particle_document(
            canonical,
            SourceRef(uri=str(filepath)),
        )
        self.ir = self.normalization.document

    def get_identifier(self):
        return self.description.get("identifier", "unknown_particle")

    def get_texture_path(self):
        """Returns the raw texture path string from the JSON (e.g. 'textures/particle/particles')"""
        params = self.description.get("basic_render_parameters", {})
        return params.get("texture", "")

    def get_material_type(self):
        params = self.description.get("basic_render_parameters", {})
        return params.get("material", "particles_alpha")

    def get_rate(self):
        """Returns (mode, amount, max_particles)"""
        if "minecraft:emitter_rate_steady" in self.components:
            rate = self.components["minecraft:emitter_rate_steady"]
            return "steady", rate.get("spawn_rate", 1), rate.get("max_particles", 50)
        elif "minecraft:emitter_rate_instant" in self.components:
            rate = self.components["minecraft:emitter_rate_instant"]
            return "instant", rate.get("num_particles", 10), rate.get("num_particles", 10)
        elif "minecraft:emitter_rate_manual" in self.components:
            rate = self.components["minecraft:emitter_rate_manual"]
            return "manual", 0, rate.get("max_particles", 50)
        return "manual", 0, 50

    def get_emitter_lifetime(self):
        """Get the lifetime of the emitter itself (how long it spawns particles)."""
        # Looping emitter
        if "minecraft:emitter_lifetime_looping" in self.components:
            data = self.components["minecraft:emitter_lifetime_looping"]
            return data.get("active_time", 10.0)
        # Once emitter
        if "minecraft:emitter_lifetime_once" in self.components:
            data = self.components["minecraft:emitter_lifetime_once"]
            return data.get("active_time", 10.0)
        # Expression-based
        if "minecraft:emitter_lifetime_expression" in self.components:
            return float('inf')
        return 10.0

    def get_particle_lifetime(self):
        """Get the lifetime of individual particles."""
        life = self.components.get("minecraft:particle_lifetime_expression", {})
        return life.get("max_lifetime", 1.0)

    def get_shape(self):
        shape_types = [
            "minecraft:emitter_shape_point",
            "minecraft:emitter_shape_sphere",
            "minecraft:emitter_shape_box",
            "minecraft:emitter_shape_disc",
            "minecraft:emitter_shape_custom",
            "minecraft:emitter_shape_entity_aabb",
        ]
        for st in shape_types:
            if st in self.components:
                return st.replace("minecraft:emitter_shape_", ""), self.components[st]
        return "point", {}

    def get_initial_speed(self):
        speed = self.components.get("minecraft:particle_initial_speed", 0.0)
        if isinstance(speed, (int, float, str, list)):
            return speed
        return 0.0

    def get_appearance_size(self):
        """Get the billboard size [width, height] of the particle."""
        appearance = self.components.get("minecraft:particle_appearance_billboard")
        if appearance is None:
            appearance = self.components.get("minecraft:particle_appearance_stretched_billboard", {})
        size = appearance.get("size", [0.1, 0.1])
        return size

    def get_uv(self):
        """Get UV information for sprite sheet animation."""
        appearance = self.components.get("minecraft:particle_appearance_billboard")
        if appearance is None:
            appearance = self.components.get("minecraft:particle_appearance_stretched_billboard", {})
        return appearance.get("uv", {})

    def get_color(self):
        """Get particle tint color."""
        tinting = self.components.get("minecraft:particle_appearance_tinting", {})
        color = tinting.get("color", None)
        return color

    def get_billboard_mode(self):
        """Returns the facing mode string from particle_appearance_billboard."""
        billboard = self.components.get("minecraft:particle_appearance_billboard")
        if billboard is not None:
            return billboard.get("facing_camera_mode", "rotate_xyz")
        
        stretched = self.components.get("minecraft:particle_appearance_stretched_billboard")
        if stretched is not None:
            # Stretched billboards usually default to a direction mode if not specified.
            # In Snowstorm, the default facing mode for stretched billboard is 'lookat_xyz' but 
            # its whole purpose is 'direction_y' or similar. We'll read it, default to rotate_xyz.
            return stretched.get("facing_camera_mode", "rotate_xyz")
            
        return "rotate_xyz"

    def get_billboard_direction(self):
        """Returns the direction dict from particle_appearance_billboard if present."""
        billboard = self.components.get("minecraft:particle_appearance_billboard")
        if billboard is not None:
            return billboard.get("direction", None)
        return None

    def has_lighting(self):
        """Returns True if the particle should be lit by the scene."""
        return "minecraft:particle_appearance_lighting" in self.components
