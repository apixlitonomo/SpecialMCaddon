"""Presentation adapter for the reusable pack query engine."""
from ..importers.pack_features import CORE, ADVANCED, SORTS, PackQuery, version
from .gpu_framework.navigation import Dropdown
from .gpu_framework.core import Style, Rect

def query_for(scene):
    flags = set(getattr(scene, 'mcbedrock_pack_filters', '').split(',')) - {''}
    exclude = getattr(scene, 'mcbedrock_pack_filter_exclude', False)
    return PackQuery(include=set() if exclude else flags, exclude=flags if exclude else set(),
        match_all=getattr(scene, 'mcbedrock_pack_match_all', False),
        minimum=version(getattr(scene, 'mcbedrock_pack_version_min', '')),
        maximum=version(getattr(scene, 'mcbedrock_pack_version_max', '')),
        sort=getattr(scene, 'mcbedrock_pack_sort', 'library'),
        descending=getattr(scene, 'mcbedrock_pack_sort_desc', False))

class PackFilterDropdown(Dropdown):
    keep_open = True
    row_height = 32

    def __init__(self, scene, state):
        self.scene, self.state = scene, state
        self.flags = set(scene.mcbedrock_pack_filters.split(',')) - {''}
        count = len(self.flags)
        choices = [('title', 'Filters & Sort' + (f' ({count})' if count else ''))]
        choices += list(CORE)
        range_label = 'Game Version Range…'
        if scene.mcbedrock_pack_version_min or scene.mcbedrock_pack_version_max:
            range_label = f'Version: {scene.mcbedrock_pack_version_min or "Any"} – {scene.mcbedrock_pack_version_max or "Any"}'
        choices += [('range', range_label), ('exclude', 'Exclude selected types'),
                    ('all', 'Require all selected types'),
                    ('advanced', ('▾' if state.get('packs.filters.advanced') else '▸')+' Advanced & Modded')]
        if state.get('packs.filters.advanced'):
            choices += list(ADVANCED)
        choices += [('sort:'+key, 'Sort: '+label) for key, label in SORTS]
        choices += [('descending', 'Descending / Newest first'), ('clear', 'Clear filters and sorting')]
        super().__init__('packs.filters', lambda ctx: 'title', self.choose, choices,
                         style=Style(width=28, height=36, font_size=11))
        self.tooltip = 'Filter and sort resource packs'

    def draw(self, renderer, state, theme):
        r = self.rect
        active = bool(self.flags or self.scene.mcbedrock_pack_version_min or
                      self.scene.mcbedrock_pack_version_max or self.scene.mcbedrock_pack_sort != 'library')
        tint = theme.accent if active else theme.text_muted
        if state.get('_menu', {}).get('key') == self.key:
            renderer.rect(r, theme.surface_hover, radius=4)
        # Compact filter glyph keeps the search usable in narrow sidebars.
        for offset, width in ((5,16),(0,10),(-5,4)):
            renderer.rect(Rect(r.x+(r.width-width)/2, r.y+r.height/2+offset, width, 2), tint, radius=1)

    def checked(self, value):
        if value in dict(CORE + ADVANCED):
            return value in self.flags
        if value == 'exclude':
            return self.scene.mcbedrock_pack_filter_exclude
        if value == 'all':
            return self.scene.mcbedrock_pack_match_all
        if value == 'descending':
            return self.scene.mcbedrock_pack_sort_desc
        if value.startswith('sort:'):
            return self.scene.mcbedrock_pack_sort == value[5:]
        return None

    def choose(self, context, value):
        scene = self.scene
        if value in dict(CORE + ADVANCED):
            self.flags.symmetric_difference_update({value})
            scene.mcbedrock_pack_filters = ','.join(sorted(self.flags))
        elif value == 'advanced':
            self.state['packs.filters.advanced'] = not self.state.get('packs.filters.advanced')
        elif value in ('exclude', 'all', 'descending'):
            prop = {'exclude': 'mcbedrock_pack_filter_exclude', 'all': 'mcbedrock_pack_match_all',
                    'descending': 'mcbedrock_pack_sort_desc'}[value]
            setattr(scene, prop, not getattr(scene, prop))
        elif value.startswith('sort:'):
            scene.mcbedrock_pack_sort = value[5:]
        elif value == 'range':
            from .styled_pages import invoke
            self.state.pop('_menu', None)
            invoke('mcbedrock.pack_version_range')(context)
        elif value == 'clear':
            scene.mcbedrock_pack_filters = ''
            scene.mcbedrock_pack_filter_exclude = scene.mcbedrock_pack_match_all = False
            scene.mcbedrock_pack_version_min = scene.mcbedrock_pack_version_max = ''
            scene.mcbedrock_pack_sort, scene.mcbedrock_pack_sort_desc = 'library', False
        self.state.pop('packs.library.scroll', None)
        self.state['_tree_dirty'] = True
