"""Shader values the day/night cycle moves every frame reach their nodes
through drivers.

Writing a node socket makes Blender update the node tree on the spot: about
0.1 ms per value in the sky's large trees, and a moving cycle wrote some 70
every frame (world, cloud copy, fog card, compositor glow). A driven socket
instead reads a custom property, which Python writes in well under a
microsecond; Blender then evaluates the drivers in one pass. A driver copies
its property unchanged, so the shaders get the same values and renders stay
pixel-identical.

Every value written here is kept in one property group (``STORE``) on the
datablock that owns its node tree: the world for its tree, a material for its
own, a node group for itself. Copies of the world's values elsewhere (the
clouds' sky fade, the fog card's sky colors) read the world's property.

Two ways to write:

* Normally the value is stored and the socket set directly, as before, so
  code reading the socket right after an update sees it.
* Inside ``per_frame()`` (the cycle's frame changes) only the property is
  stored. A channel that changes there gets a driver the first time, so only
  what the cycle moves is driven; everything else stays a plain value.
  Read values back with ``value()``, not ``socket.default_value``.

While a render job runs, no driver is added (that changes the data the render
is reading); such writes go to the socket directly instead.
"""
import contextlib
import hashlib
import struct

import bpy

STORE = 'sparkle_driven'
# Sockets whose values are floats: one driver per channel.
_DRIVABLE = {'VALUE', 'RGBA', 'VECTOR'}

_fast = 0
_owners = {}    # embedded node tree pointer -> its world or material
_driven = {}    # (host pointer, path) -> channels with a working driver, this session
_moving = {}    # (owner pointer, key) -> channels the cycle changed, this session
_generation = [0]  # bumped whenever a channel starts moving: copies then follow it


@contextlib.contextmanager
def per_frame():
    """Writes inside reach sockets through their drivers (see module doc)."""
    global _fast
    _fast += 1
    try:
        yield
    finally:
        _fast -= 1


def in_per_frame():
    return _fast > 0


def generation():
    """Changes whenever another value starts moving with the cycle."""
    return _generation[0]


def clear():
    """Forget looked-up owners and drivers (after load, undo and rebuilds)."""
    _owners.clear()
    _driven.clear()
    _moving.clear()
    _generation[0] += 1


def owner_of(tree):
    """The datablock a node tree's values are stored on: the world or material
    holding an embedded tree, else the node group itself; None if unknown."""
    if not getattr(tree, 'is_embedded_data', False):
        return tree
    pointer = tree.as_pointer()
    owner = _owners.get(pointer)
    try:
        if owner is not None and owner.node_tree is not None and owner.node_tree.as_pointer() == pointer:
            return owner
    except ReferenceError:
        pass
    for collection in (bpy.data.worlds, bpy.data.materials):
        for item in collection:
            if item.node_tree is not None and item.node_tree.as_pointer() == pointer:
                _owners[pointer] = item
                return item
    return None


def _key(path):
    key = path.replace('"', "'")
    if len(key) > 63:  # the longest property name Blender keeps
        key = 'sparkle ' + hashlib.sha1(key.encode('utf-8')).hexdigest()[:20]
    return key


def key_of(target, name):
    """The property name for one of a node tree's values (its RNA path)."""
    return _key(target.path_from_id(name))


def drivable(socket):
    return getattr(socket, 'type', '') in _DRIVABLE and hasattr(socket, 'default_value')


def _f32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def _plain(value):
    """A float, or a tuple of floats, at the precision sockets keep."""
    if hasattr(value, '__len__') and not isinstance(value, str):
        return tuple(_f32(float(channel)) for channel in value)
    return _f32(float(value))


def _store(owner):
    store = owner.get(STORE)
    if store is None:
        owner[STORE] = {}
        store = owner[STORE]
    return store


def stored(owner, key):
    """The stored value under ``key`` on ``owner``, or None."""
    store = owner.get(STORE) if owner is not None else None
    value = store.get(key) if store is not None else None
    if value is None:
        return None
    return tuple(value) if hasattr(value, '__len__') else value


def _same(old, new, tolerance=0.):
    if old is None:
        return False
    if isinstance(new, tuple):
        return len(old) == len(new) and all(abs(a - b) <= tolerance for a, b in zip(old, new))
    return abs(old - new) <= tolerance


def _changed(old, new, tolerance):
    """The channels of ``new`` that differ from ``old`` (all when there is no old)."""
    if not isinstance(new, tuple):
        return () if _same(old, new, tolerance) else (0,)
    if old is None or len(old) != len(new):
        return tuple(range(len(new)))
    return tuple(i for i, (a, b) in enumerate(zip(old, new)) if abs(a - b) > tolerance)


def _set_direct(target, name, value, tolerance=0.):
    if not _same(_plain(getattr(target, name)), value, tolerance):
        setattr(target, name, value)


def _rendering():
    try:
        return bpy.app.is_job_running('RENDER')
    except (AttributeError, TypeError):
        return False


def _drive(host, path, owner, key, channels, array):
    """Drivers on ``host``'s value at ``path`` for ``channels``, reading
    ``owner[STORE][key]``. False when a render is running and none could be added."""
    known = _driven.setdefault((host.as_pointer(), path), set())
    missing = [index for index in channels if index not in known]
    if not missing:
        return True
    anim = host.animation_data
    drivers = anim.drivers if anim is not None else None
    adding = [index for index in missing if drivers is None or drivers.find(path, index=index) is None]
    if adding and _rendering():
        return False
    prop = '["%s"]["%s"]' % (STORE, key)
    for index in missing:
        curve = drivers.find(path, index=index) if drivers is not None else None
        if curve is None:
            curve = host.driver_add(path, index) if array else host.driver_add(path)
            drivers = host.animation_data.drivers
        # A new driver's curve maps its value through keyframes (0, 0) and (1, 1),
        # which rounds some values by a float step. Without them it passes unchanged.
        curve.keyframe_points.clear()
        while len(curve.modifiers):
            curve.modifiers.remove(curve.modifiers[0])
        driver = curve.driver
        driver.type = 'AVERAGE'
        variables = driver.variables
        while len(variables) > 1:
            variables.remove(variables[-1])
        variable = variables[0] if len(variables) else variables.new()
        variable.name = 'value'
        variable.type = 'SINGLE_PROP'
        target = variable.targets[0]
        target.id_type = owner.id_type
        target.id = owner
        target.data_path = prop + ('[%d]' % index if array else '')
        # A driver that once failed (its property was missing) stays skipped until reset.
        try:
            driver.is_valid = curve.is_valid = True
        except (AttributeError, TypeError):
            pass
        known.add(index)
    return True


def _mark_moving(owner, key, channels):
    moving = _moving.setdefault((owner.as_pointer(), key), set())
    if not moving.issuperset(channels):
        moving.update(channels)
        _generation[0] += 1


def write(target, name, value, owner=None, tolerance=0.):
    """Set a node socket's default value to ``value``, through its drivers
    inside ``per_frame()`` (see module doc). Changes within ``tolerance`` are
    skipped, as callers did before. False when ``target`` has no owner to
    store on, after setting it directly."""
    value = _plain(value)
    host = target.id_data
    owner = owner if owner is not None else owner_of(host)
    if owner is None:
        _set_direct(target, name, value, tolerance)
        return False
    path = target.path_from_id(name)
    key = _key(path)
    old = stored(owner, key)
    channels = _changed(old, value, tolerance)
    if channels:
        _store(owner)[key] = value
        owner.update_tag()
    elif old is not None:
        value = old  # what the socket holds or is driven to
    array = isinstance(value, tuple)
    # A value's first write is never driven: only one that changes from one
    # cycle frame to the next is. (Sockets other code also writes, such as
    # the compositor's contrast, never move with the cycle.)
    if _fast and channels and old is not None:
        _mark_moving(owner, key, channels)
        if _drive(host, path, owner, key, channels, array):
            return True
    if not _fast or channels:
        _set_direct(target, name, value, tolerance)
    return True


def publish(target, name, value, owner=None):
    """Set ``target`` directly, and store its value where copies can follow it.
    For the sky's color ramp: fog reads that ramp in Python every frame, so it
    must hold the current stops at once."""
    value = _plain(value)
    owner = owner if owner is not None else owner_of(target.id_data)
    if owner is not None:
        key = key_of(target, name)
        old = stored(owner, key)
        channels = _changed(old, value, 0.)
        if channels:
            _store(owner)[key] = value
            owner.update_tag()
            if _fast and old is not None:
                _mark_moving(owner, key, channels)
    _set_direct(target, name, value)


def follow(target, name, owner, key, tolerance=0.):
    """Keep ``target`` a copy of the value stored under ``key`` on ``owner``
    (the clouds and the fog card mirror the world's sky): copied directly, and
    driven for the channels the cycle moves. ``tolerance`` spares direct
    copies of tiny changes. False when ``owner`` keeps no such value."""
    value = stored(owner, key)
    if value is None:
        return False
    if _fast:
        moving = _moving.get((owner.as_pointer(), key))
        if moving and _drive(target.id_data, target.path_from_id(name), owner, key,
                             tuple(sorted(moving)), isinstance(value, tuple)):
            return True
    _set_direct(target, name, value, tolerance)
    return True


def value(target, name='default_value'):
    """A node value as last written: the stored one when it is driven (the
    socket itself catches up when Blender next evaluates)."""
    owner = owner_of(target.id_data)
    if owner is not None:
        found = stored(owner, key_of(target, name))
        if found is not None:
            return found
    current = getattr(target, name)
    return tuple(current) if hasattr(current, '__len__') else current
