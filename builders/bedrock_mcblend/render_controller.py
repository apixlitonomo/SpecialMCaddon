"""Small, conservative render-controller helpers for Bedrock entity imports."""

import math
import os
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

try:
    from ...importers.bedrock_assets import load_json_file
    from ...importers.bedrock_materials import (
        behavior_fields_from_definition,
        normalize_material_name,
    )
    from ...importers.bedrock_compatibility import material_fallback_issue
except ImportError:  # pragma: no cover - plain Python test path
    from importers.bedrock_assets import load_json_file
    from importers.bedrock_compatibility import material_fallback_issue
    from importers.bedrock_materials import (
        behavior_fields_from_definition,
        normalize_material_name,
    )


_ASSIGN_RE = re.compile(
    r"\b(?:v|variable)\.([A-Za-z0-9_]+)\s*=\s*(true|false|-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_VAR_RE = re.compile(r"\b(?:v|variable)\.([A-Za-z0-9_]+)\b")
_QUERY_RE = re.compile(r"\b(?:q|query)\.([A-Za-z0-9_]+)\b", re.IGNORECASE)
_REF_RE = re.compile(r"\b(Geometry|Texture|Material)\.([A-Za-z0-9_]+)\b", re.IGNORECASE)
_TERNARY_EXPR_RE = re.compile(
    r"^(?P<cond>.+?)\?(?P<true>.+?):(?P<false>.+)$"
)
_ARRAY_RESOURCE_RE = re.compile(
    r"^Array\.([A-Za-z0-9_]+)(?:\[([^\]]+)\])?$", re.IGNORECASE
)
_TERNARY_COND_VAR_RE = re.compile(r"\b(?:v|variable)\.([A-Za-z0-9_]+)\s*\?")


def _strip_outer_parentheses(text):
    text = str(text).strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        encloses_all = True
        for index, character in enumerate(text):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        text = text[1:-1].strip()
    return text


def _split_top_level_ternary(text):
    """Split one balanced MoLang ternary without consuming nested colons."""
    text = _strip_outer_parentheses(text)
    depth = 0
    question = -1
    for index, character in enumerate(text):
        if character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        elif character == "?" and depth == 0:
            question = index
            break
    if question < 0:
        return None
    depth = 0
    nested = 0
    for index in range(question + 1, len(text)):
        character = text[index]
        if character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and character == "?":
            nested += 1
        elif depth == 0 and character == ":":
            if nested:
                nested -= 1
            else:
                return (
                    text[:question].strip(),
                    text[question + 1:index].strip(),
                    text[index + 1:].strip(),
                )
    return None


def _iter_initialize_expressions(description):
    scripts = description.get("scripts", {}) if isinstance(description, dict) else {}
    initialize = scripts.get("initialize", []) if isinstance(scripts, dict) else []
    if isinstance(initialize, (str, dict)):
        initialize = [initialize]

    def walk(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(item)

    yield from walk(initialize)


def initialized_variables(client_description):
    variables = {}
    for entry in _iter_initialize_expressions(client_description):
        for match in _ASSIGN_RE.finditer(entry):
            raw_value = match.group(2).lower()
            if raw_value == "true":
                value = 1.0
            elif raw_value == "false":
                value = 0.0
            else:
                value = float(raw_value)
            variables[match.group(1)] = value
    return variables


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return abs(float(value)) > 1.0e-6
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1"}:
            return True
        if text in {"false", "0"}:
            return False
    return None


_Q_VARIANT_CMP_RE = re.compile(r"^(?:q|query)\.variant\s*(==|!=)\s*(-?\d+)$")
_V_VALUE_CMP_RE = re.compile(
    r"^(?:v|variable)\.([A-Za-z0-9_]+)\s*(==|!=|<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)
_Q_VALUE_CMP_RE = re.compile(
    r"^(?:q|query)\.([A-Za-z0-9_]+)\s*(==|!=|<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)$"
)
_Q_ANY_VARIANT_RE = re.compile(r"^(?:q|query)\.any\(\s*(?:q|query)\.variant\s*,([^)]*)\)$")
_Q_ATOM_RE = re.compile(r"^(?:q|query)\.([A-Za-z0-9_]+)$")

# Idle/rest defaults: pose queries are false unless a preview context sets them.
# Without this, armadillo rolled + unrolled bones both stay visible at rest.
_REST_FALSE_QUERIES = {
    "is_rolling", "is_rolled_up", "is_swimming", "is_sleeping", "is_sitting",
    "is_baby", "is_gliding", "is_climbing", "is_sneaking", "is_sprinting",
    "is_charging", "is_angry", "is_scared", "is_stunned", "is_illager_captain",
    "has_target", "is_moving", "is_alive",  # is_alive kept True below
}


def _eval_condition_atom(text, variables, query_context, assume_query_false=False):
    """Evaluate a single !-prefixed atom: v.* variable or q.* comparison."""
    text = _strip_outer_parentheses(text)
    negated = False
    while text.startswith("!"):
        negated = not negated
        text = _strip_outer_parentheses(text[1:])

    result = None
    value_match = _V_VALUE_CMP_RE.fullmatch(text)
    if value_match and value_match.group(1) in variables:
        try:
            left = float(variables[value_match.group(1)])
            right = float(value_match.group(3))
            operator = value_match.group(2)
            result = {
                "==": left == right,
                "!=": left != right,
                "<=": left <= right,
                ">=": left >= right,
                "<": left < right,
                ">": left > right,
            }[operator]
        except (TypeError, ValueError):
            result = None
    match = _VAR_RE.fullmatch(text)
    if result is None and match:
        name = match.group(1)
        if name in variables:
            result = abs(float(variables[name])) > 1.0e-6
        elif assume_query_false:
            # Rest pose: pre_animation vars (e.g. armadillo use_rolled_up_model)
            # are unset — treat as false so alternate body states stay hidden.
            result = False
    elif result is None and query_context is not None:
        value_match = _Q_VALUE_CMP_RE.fullmatch(text)
        if value_match and value_match.group(1) in query_context:
            try:
                left = float(query_context[value_match.group(1)])
                right = float(value_match.group(3))
                operator = value_match.group(2)
                result = {
                    "==": left == right,
                    "!=": left != right,
                    "<=": left <= right,
                    ">=": left >= right,
                    "<": left < right,
                    ">": left > right,
                }[operator]
            except (TypeError, ValueError):
                result = None
        if "variant" in query_context:
            variant = query_context["variant"]
            cmp_match = _Q_VARIANT_CMP_RE.fullmatch(text)
            if cmp_match:
                equal = int(cmp_match.group(2)) == variant
                result = equal if cmp_match.group(1) == "==" else not equal
            else:
                any_match = _Q_ANY_VARIANT_RE.fullmatch(text)
                if any_match:
                    values = []
                    for token in any_match.group(1).split(","):
                        try:
                            values.append(int(token.strip()))
                        except ValueError:
                            return None
                    result = variant in values
        if result is None:
            q_match = _Q_ATOM_RE.fullmatch(text)
            if q_match and q_match.group(1) in query_context:
                result = _coerce_bool(query_context[q_match.group(1)])

    if result is None and assume_query_false:
        q_match = _Q_ATOM_RE.fullmatch(text)
        if q_match:
            q_name = q_match.group(1).lower()
            if q_name == "is_alive":
                result = True
            elif q_name in _REST_FALSE_QUERIES or q_name.startswith("is_") or q_name.startswith("has_"):
                result = False

    if result is None:
        return None
    return not result if negated else result


def evaluate_visibility(expression, variables, query_context=None, assume_query_false=False):
    """Return True/False for deterministic expressions, or None if unknown.

    Supports numeric/boolean literals, ``v.*`` variables, ``q.variant``
    equality tests (when a preview ``query_context`` supplies a variant), and
    ``||`` / ``&&`` combinations of those atoms.

    When ``assume_query_false`` is set (rest-pose part visibility), unknown
    ``q.is_*`` / ``q.has_*`` atoms and unset ``v.*`` variables default to false
    so alternate poses (rolled armadillo, sleeping, etc.) stay hidden.
    """
    direct = _coerce_bool(expression)
    if direct is not None:
        return direct
    if not isinstance(expression, str):
        return None
    text = expression.strip()
    text = _strip_outer_parentheses(text)
    if not text:
        return None

    if "||" in text:
        results = [
            evaluate_visibility(part.strip(), variables, query_context, assume_query_false)
            for part in text.split("||")
        ]
        if any(r is True for r in results):
            return True
        if all(r is False for r in results):
            return False
        return None
    if "&&" in text:
        results = [
            evaluate_visibility(part.strip(), variables, query_context, assume_query_false)
            for part in text.split("&&")
        ]
        if any(r is False for r in results):
            return False
        if all(r is True for r in results):
            return True
        return None
    return _eval_condition_atom(text, variables, query_context, assume_query_false)


def hidden_parts_from_visibility(part_visibility, variables, query_context=None, assume_query_false=False):
    hidden = set()
    if not isinstance(part_visibility, list):
        return hidden
    for entry in part_visibility:
        if not isinstance(entry, dict):
            continue
        for bone_name, expression in entry.items():
            result = evaluate_visibility(
                expression, variables, query_context, assume_query_false=assume_query_false
            )
            if result is False:
                hidden.add(str(bone_name))
            elif result is True:
                bone_name = str(bone_name)
                hidden.discard(bone_name)
                if any(fnmatchcase(bone_name.lower(), pattern.lower()) for pattern in hidden):
                    hidden.add("!" + bone_name)
    return hidden


def part_is_hidden(name, bones_by_name, hidden_parts):
    """Match case-insensitive controller part patterns through ancestors."""
    patterns = tuple(str(pattern).lower() for pattern in (hidden_parts or ()))
    visible_overrides = tuple(
        pattern[1:] for pattern in patterns if pattern.startswith("!")
    )
    hidden_patterns = tuple(pattern for pattern in patterns if not pattern.startswith("!"))
    current = str(name)
    seen = set()
    while current and current not in seen:
        lowered = current.lower()
        if any(fnmatchcase(lowered, pattern) for pattern in visible_overrides):
            return False
        if any(fnmatchcase(lowered, pattern) for pattern in hidden_patterns):
            return True
        seen.add(current)
        bone = (
            bones_by_name.get(current, {})
            if isinstance(bones_by_name, dict) else {}
        )
        parent = bone.get("parent") if isinstance(bone, dict) else ""
        current = parent if isinstance(parent, str) else ""
    return False


def script_animation_entries(client_description):
    """Yield ``(alias, condition)`` pairs from ``scripts.animate``."""
    scripts = client_description.get("scripts", {}) if isinstance(client_description, dict) else {}
    animate = scripts.get("animate", []) if isinstance(scripts, dict) else []
    if isinstance(animate, (str, dict)):
        animate = [animate]
    if not isinstance(animate, list):
        return
    for entry in animate:
        if isinstance(entry, str):
            yield entry, True
        elif isinstance(entry, dict):
            for alias, condition in entry.items():
                yield str(alias), condition


def _preview_context_from_condition(condition):
    """Infer only query values forced by one selected-animation condition."""
    direct = _coerce_bool(condition)
    if direct is True:
        return {}
    if not isinstance(condition, str):
        return None

    text = condition.strip()
    if not text:
        return None

    # An OR does not identify one boolean state. Preserve the existing,
    # deterministic variant preview by selecting its first concrete branch.
    if "||" in text:
        for part in text.split("||"):
            part = part.strip()
            cmp_match = _Q_VARIANT_CMP_RE.fullmatch(part)
            if cmp_match and cmp_match.group(1) == "==":
                return {"variant": int(cmp_match.group(2))}
            any_match = _Q_ANY_VARIANT_RE.fullmatch(part)
            if any_match:
                try:
                    return {"variant": int(any_match.group(1).split(",")[0].strip())}
                except (ValueError, IndexError):
                    continue
        return None

    context = {}
    for atom in text.split("&&"):
        atom = atom.strip()
        cmp_match = _Q_VARIANT_CMP_RE.fullmatch(atom)
        if cmp_match:
            if cmp_match.group(1) != "==":
                return None
            variant = int(cmp_match.group(2))
            if "variant" in context and context["variant"] != variant:
                return None
            context["variant"] = variant
            continue

        any_match = _Q_ANY_VARIANT_RE.fullmatch(atom)
        if any_match:
            try:
                context["variant"] = int(any_match.group(1).split(",")[0].strip())
            except (ValueError, IndexError):
                return None
            continue

        negated = False
        while atom.startswith("!"):
            negated = not negated
            atom = atom[1:].strip()
        q_match = _Q_ATOM_RE.fullmatch(atom)
        if not q_match:
            return None
        query_name = q_match.group(1)
        value = not negated
        if query_name in context and context[query_name] != value:
            return None
        context[query_name] = value

    return context or None


def infer_preview_context(client_description, animation_filter):
    """Infer deterministic query values for a selected animation preview.

    Reverse-maps ``animation_filter`` to its ``scripts.animate`` alias. Simple
    boolean query gates and concrete ``q.variant`` branches become preview
    context. An unconditional selected animation returns an empty context so
    optional runtime controllers stay excluded.
    """
    if not animation_filter or not isinstance(client_description, dict):
        return None
    alias_table = client_description.get("animations", {})
    alias_table = alias_table if isinstance(alias_table, dict) else {}
    aliases = {alias for alias, ref in alias_table.items() if ref == animation_filter}
    aliases.add(animation_filter)

    for alias, condition in script_animation_entries(client_description):
        if alias not in aliases:
            continue
        context = _preview_context_from_condition(condition)
        if context is not None:
            return context
    return None


def resolve_import_profile(animation_filter, import_profile, preview_context=None):
    """Choose geometry independently from the animation action to apply."""
    if import_profile == "all":
        return "all"
    selected_animation = animation_filter not in ("__NONE__", "__ALL__", "")
    if selected_animation and preview_context is not None:
        return "animation"
    return "rest"


def _controller_activation(client_description, variables=None, query_context=None):
    """Map render-controller id -> activation kind for import filtering.

    Returns {controller_id: "unconditional" | "true" | "false" | "unknown"}.
    """
    variables = variables or {}
    activation = {}
    priority = {"false": 0, "unknown": 1, "true": 2, "unconditional": 3}

    def record(controller_id, kind):
        controller_id = str(controller_id)
        previous = activation.get(controller_id)
        if previous is None or priority[kind] > priority[previous]:
            activation[controller_id] = kind

    render_controllers = client_description.get("render_controllers", []) if isinstance(client_description, dict) else []
    if isinstance(render_controllers, str):
        render_controllers = [render_controllers]
    if not isinstance(render_controllers, list):
        return activation
    for entry in render_controllers:
        if isinstance(entry, str):
            record(entry, "unconditional")
            continue
        if not isinstance(entry, dict):
            continue
        for controller_id, condition in entry.items():
            result = evaluate_visibility(condition, variables, query_context)
            if result is True:
                kind = "true"
            elif result is False:
                kind = "false"
            else:
                kind = "unknown"
            record(controller_id, kind)
    return activation


def iter_render_controller_refs(client_description, variables=None, import_profile="rest", query_context=None):
    """Yield render-controller ids selected by a client entity description.

    - ``rest``: only unconditional controllers and deterministic-true conditions.
      Unknown runtime ``q.*`` / missing ``v.*`` conditions are excluded so
      animation props and attachables do not spawn at rest pose.
    - ``animation`` with a ``query_context`` (inferred variant): strict like
      rest, so only controllers whose conditions hold for that variant.
    - ``animation`` without context / ``all``: include unknown conditions
      (not deterministically false).
    """
    activation = _controller_activation(client_description, variables, query_context)
    strict = import_profile == "rest" or (import_profile == "animation" and query_context is not None)
    expand_all = import_profile == "all"
    
    for controller_id, kind in activation.items():
        if expand_all:
            yield controller_id
            continue
            
        if kind == "false":
            continue
        if strict:
            if kind in {"unconditional", "true"}:
                yield controller_id
            continue
        yield controller_id


_RENDER_CONTROLLER_INDEX = {}


def _render_controller_index(root):
    key = os.path.normcase(os.path.abspath(root or ""))
    cached = _RENDER_CONTROLLER_INDEX.get(key)
    if cached is not None:
        return cached
    index = {}
    candidates = [
        os.path.join(root, "render_controllers"),
        os.path.join(root, "render_controller"),
    ]
    scan_roots = [path for path in candidates if os.path.isdir(path)]
    if not scan_roots:
        _RENDER_CONTROLLER_INDEX[key] = index
        return index
    for scan_root in scan_roots:
        for current, dirs, files in os.walk(scan_root):
            dirs[:] = [
                name for name in dirs
                if name not in {"__MACOSX", ".git", "node_modules", "__pycache__"}
            ]
            for filename in files:
                if not filename.lower().endswith(".json"):
                    continue
                filepath = os.path.join(current, filename)
                try:
                    data = load_json_file(filepath)
                except Exception:
                    continue
                controllers = data.get("render_controllers", {}) if isinstance(data, dict) else {}
                if not isinstance(controllers, dict):
                    continue
                for identifier, controller in controllers.items():
                    if isinstance(controller, dict):
                        index.setdefault(str(identifier).lower(), (filepath, controller))
    _RENDER_CONTROLLER_INDEX[key] = index
    return index


def clear_render_controller_cache():
    _RENDER_CONTROLLER_INDEX.clear()


def find_render_controller(search_roots, controller_id):
    if not controller_id:
        return "", {}
    target = str(controller_id).lower()
    for root in search_roots:
        result = _render_controller_index(root).get(target)
        if result:
            return result
    return "", {}


def _refs_from_expression(expression, kind):
    refs = []
    if isinstance(expression, str):
        for ref_kind, ref_name in _REF_RE.findall(expression):
            if ref_kind.casefold() == kind.casefold() and ref_name not in refs:
                refs.append(ref_name)
    elif isinstance(expression, list):
        for item in expression:
            for ref in _refs_from_expression(item, kind):
                if ref not in refs:
                    refs.append(ref)
    elif isinstance(expression, dict):
        for item in expression.values():
            for ref in _refs_from_expression(item, kind):
                if ref not in refs:
                    refs.append(ref)
    return refs


def _ref_name_from_token(token, kind):
    prefix = f"{kind}."
    if isinstance(token, str) and token.casefold().startswith(prefix.casefold()):
        return token[len(prefix):]
    return ""


def _array_resource_refs(controller, kind, array_name):
    """Return resource keys from a named render-controller array."""
    bucket_name = {"Geometry": "geometries", "Texture": "textures", "Material": "materials"}[kind]
    arrays = controller.get("arrays", {}) if isinstance(controller, dict) else {}
    resources = arrays.get(bucket_name, {}) if isinstance(arrays, dict) else {}
    if not isinstance(resources, dict):
        return []
    wanted_names = {
        str(array_name).casefold(),
        f"array.{array_name}".casefold(),
    }
    values = next(
        (value for key, value in resources.items() if str(key).casefold() in wanted_names),
        [],
    )
    if not isinstance(values, list):
        return []
    return [
        ref for ref in (_ref_name_from_token(value, kind) for value in values) if ref
    ]


def _resolve_resource_ref(
    expression,
    kind,
    variables,
    query_context,
    controller=None,
    assume_query_false=False,
):
    """Resolve ordered Geometry/Texture/Material keys, including arrays."""
    if isinstance(expression, list):
        resolved = []
        for item in expression:
            resolved.extend(_resolve_resource_ref(
                item, kind, variables, query_context,
                controller=controller, assume_query_false=assume_query_false,
            ))
        return resolved
    if not isinstance(expression, str):
        return _refs_from_expression(expression, kind)[:1]

    text = _strip_outer_parentheses(expression)
    ternary = _split_top_level_ternary(text)
    if ternary:
        picked = evaluate_visibility(
            ternary[0],
            variables,
            query_context,
            assume_query_false=assume_query_false,
        )
        if picked is None:
            return []
        branch = ternary[1] if picked is True else ternary[2]
        return _resolve_resource_ref(
            branch.strip(), kind, variables, query_context,
            controller=controller, assume_query_false=assume_query_false,
        )

    array_match = _ARRAY_RESOURCE_RE.match(text)
    if array_match:
        refs = _array_resource_refs(controller, kind, array_match.group(1))
        if not refs:
            return []
        index_expression = (array_match.group(2) or "").strip()
        if not index_expression:
            return [] if len(refs) != 1 else [refs[0]]
        try:
            index = int(float(index_expression))
        except ValueError:
            value = None
            variable_match = _VAR_RE.fullmatch(index_expression)
            if variable_match:
                name = variable_match.group(1)
                if name in variables:
                    value = variables[name]
                elif query_context is not None and name in query_context:
                    value = query_context[name]
            else:
                query_match = _QUERY_RE.fullmatch(index_expression)
                if (
                    query_match
                    and query_context is not None
                    and query_match.group(1) in query_context
                ):
                    value = query_context[query_match.group(1)]
                elif query_context is not None:
                    variable_names = set(_VAR_RE.findall(index_expression))
                    query_names = set(_QUERY_RE.findall(index_expression))
                    if (
                        variable_names.issubset(set(variables))
                        and query_names.issubset(set(query_context))
                    ):
                        try:
                            from ...molang_parser import evaluate as evaluate_molang
                        except ImportError:
                            from molang_parser import evaluate as evaluate_molang
                        value = evaluate_molang(
                            index_expression,
                            {"v": dict(variables), "q": dict(query_context)},
                            None,
                        )
            if value is None:
                # Runtime variables and properties may be unavailable while a
                # resource pack is being previewed. Bedrock arrays are zero
                # based and authors put the default visual in the first slot,
                # so keep that authored fallback instead of dropping the
                # geometry/texture/material reference entirely.
                index = 0
            else:
                try:
                    index = int(float(value))
                except (TypeError, ValueError):
                    index = 0
        return [refs[max(0, min(index, len(refs) - 1))]]

    refs = _refs_from_expression(text, kind)
    if len(refs) > 1:
        return [refs[-1]]
    return refs


def _augment_preview_variables(variables, controller_ids, controller_lookup, query_context):
    """Enable outfit ternaries (v.var ? costume : default) for variant-gated previews."""
    if not query_context or "variant" not in query_context:
        return variables
    augmented = dict(variables)
    for controller_id in controller_ids:
        controller = controller_lookup(controller_id) or {}
        if not isinstance(controller, dict):
            continue
        for field in ("geometry", "textures"):
            value = controller.get(field, "")
            if isinstance(value, list):
                text = value[0] if value else ""
            else:
                text = value
            if not isinstance(text, str):
                continue
            for var_match in _TERNARY_COND_VAR_RE.finditer(text):
                augmented[var_match.group(1)] = 1.0
    return augmented


def _array_texture_refs(controller):
    refs = []
    arrays = controller.get("arrays", {}) if isinstance(controller, dict) else {}
    textures = arrays.get("textures", {}) if isinstance(arrays, dict) else {}
    if not isinstance(textures, dict):
        return refs
    for values in textures.values():
        for ref in _refs_from_expression(values, "Texture"):
            if ref not in refs:
                refs.append(ref)
    return refs



def _static_controller_number(value, variables, query_context):
    """Evaluate a controller scalar only when every dependency is known."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if not isinstance(value, str) or not value.strip():
        return None
    variable_names = set(_VAR_RE.findall(value))
    query_names = set(_QUERY_RE.findall(value))
    if not variable_names.issubset(set(variables)):
        return None
    if query_context is None or not query_names.issubset(set(query_context)):
        return None
    try:
        from ...molang_parser import evaluate as evaluate_molang
    except ImportError:
        from molang_parser import evaluate as evaluate_molang
    try:
        number = float(evaluate_molang(
            value,
            {"v": dict(variables), "q": dict(query_context)},
            None,
        ))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return number if math.isfinite(number) else None


def _controller_material_metadata(
    controller,
    controller_id,
    variables,
    query_context,
):
    """Resolve source-declared shading and static UV animation state."""
    metadata = {
        "ignore_lighting": False,
        "light_color_multiplier": 1.0,
        "uv_offset": (0.0, 0.0),
        "uv_scale": (1.0, 1.0),
        "issues": [],
    }
    if not isinstance(controller, dict):
        return metadata

    source_root = f"$.render_controllers.{controller_id}"

    def unsupported(field, fallback):
        metadata["issues"].append(material_fallback_issue(
            f"Render-controller field {field!r} is not statically evaluable",
            fallback=fallback,
            source_location=f"{source_root}.{field}",
        ).as_dict())

    if "ignore_lighting" in controller:
        value = controller.get("ignore_lighting")
        if isinstance(value, bool):
            metadata["ignore_lighting"] = value
        else:
            number = _static_controller_number(value, variables, query_context)
            if number is None:
                unsupported("ignore_lighting", "lit material")
            else:
                metadata["ignore_lighting"] = abs(number) > 1.0e-6

    if "light_color_multiplier" in controller:
        number = _static_controller_number(
            controller.get("light_color_multiplier"), variables, query_context
        )
        if number is None or number < 0.0:
            unsupported("light_color_multiplier", "multiplier 1.0")
        else:
            metadata["light_color_multiplier"] = number

    uv_anim = controller.get("uv_anim")
    if uv_anim is not None:
        if not isinstance(uv_anim, dict):
            unsupported("uv_anim", "offset [0, 0] and scale [1, 1]")
        else:
            for source_key, target_key, fallback in (
                ("offset", "uv_offset", (0.0, 0.0)),
                ("scale", "uv_scale", (1.0, 1.0)),
            ):
                raw = uv_anim.get(source_key, list(fallback))
                values = raw if isinstance(raw, (list, tuple)) else []
                resolved = (
                    [_static_controller_number(item, variables, query_context) for item in values]
                    if len(values) == 2
                    else []
                )
                if len(resolved) != 2 or any(item is None for item in resolved):
                    unsupported(
                        f"uv_anim.{source_key}",
                        f"{source_key} {list(fallback)}",
                    )
                else:
                    metadata[target_key] = tuple(resolved)
    return metadata

def _material_hints_from_controller(controller):
    """Collect Material.* tokens referenced by a render controller."""
    hints = []
    if not isinstance(controller, dict):
        return hints
    materials = controller.get("materials", [])
    if isinstance(materials, dict):
        materials = [materials]
    if not isinstance(materials, list):
        return hints
    for entry in materials:
        for ref in _refs_from_expression(entry, "Material"):
            if ref not in hints:
                hints.append(ref)
    return hints


@dataclass(frozen=True)
class MaterialBehavior:
    """Resolved behavior of one declared client material."""

    key: str
    name: str
    surface_mode: str
    cutout_threshold: float
    legacy_emission_mode: str
    culling: str
    alpha_role: str
    lighting: str
    emission: str
    provenance: tuple
    known: bool
    issues: tuple = ()

    @property
    def render_method(self):
        """Compatibility projection used by older importer call sites."""
        if self.surface_mode == "blend":
            return "blend"
        if self.surface_mode == "cutout":
            return (
                "emissive_alpha_test"
                if self.legacy_emission_mode == "inverse_alpha"
                else "alpha_test"
            )
        return "opaque"

    @property
    def cull_backfaces(self):
        return self.culling == "back"

    def as_dict(self):
        return {
            "key": self.key,
            "name": self.name,
            "surface_mode": self.surface_mode,
            "cutout_threshold": self.cutout_threshold,
            "legacy_emission_mode": self.legacy_emission_mode,
            "alpha_role": self.alpha_role,
            "lighting": self.lighting,
            "emission": self.emission,
            "provenance": list(self.provenance),
            "culling": self.culling,
            "render_method": self.render_method,
            "cull_backfaces": self.cull_backfaces,
            "known": self.known,
            "issues": list(self.issues),
        }


_MATERIAL_BEHAVIORS = {
    # Normalized engine families; pack-local definitions take precedence.
    "entity": ("opaque", 0.0, "none", "none", "data", "lit", "none"),
    "entity_alphatest": ("cutout", 0.5, "none", "none", "coverage", "lit", "none"),
    "entity_alphatest_one_sided": ("cutout", 0.5, "none", "back", "coverage", "lit", "none"),
    "entity_alphablend": ("blend", 0.0, "none", "back", "coverage", "lit", "none"),
    "entity_emissive": ("opaque", 0.0, "inverse_alpha", "none", "data", "lit", "inverse_alpha"),
    "entity_emissive_alpha": ("cutout", 1.0e-4, "inverse_alpha", "none", "coverage", "lit", "inverse_alpha"),
    "entity_emissive_layer_alpha_test": ("cutout", 0.5, "none", "none", "coverage", "lit", "none"),
    "entity_nocull": ("opaque", 0.0, "none", "none", "data", "lit", "none"),
    "entity_alphatest_multicolor_tint": ("cutout", 0.5, "none", "none", "tint_mask", "lit", "none"),
}



def _declared_material(client_description, material_keys):
    materials = (
        client_description.get("materials", {})
        if isinstance(client_description, dict)
        else {}
    )
    materials = materials if isinstance(materials, dict) else {}
    by_casefold = {
        str(key).casefold(): (str(key), value) for key, value in materials.items()
    }
    explicit_keys = []
    for key in material_keys or []:
        key = str(key)
        if key.casefold() not in {item.casefold() for item in explicit_keys}:
            explicit_keys.append(key)
    keys = explicit_keys or ["default", "*"]
    for key in keys:
        actual_key, value = by_casefold.get(key.casefold(), (key, None))
        if isinstance(value, str) and value.strip():
            return actual_key, value.strip()
    return (explicit_keys[0], "") if explicit_keys else ("", "")


def _fallback_behavior(key, name, message, culling="none"):
    issue = material_fallback_issue(
        message,
        fallback=(
            "two-sided alpha-test rendering; the declared source remains "
            "recorded"
        ),
        source_location=(
            f"$.description.materials.{key}"
            if key
            else "$.description.materials"
        ),
    ).as_dict()
    return MaterialBehavior(
        key=key,
        name=name,
        surface_mode="cutout",
        cutout_threshold=0.5,
        legacy_emission_mode="none",
        culling=culling,
        alpha_role="coverage",
        lighting="lit",
        emission="none",
        provenance=("diagnosed_fallback",),
        known=False,
        issues=(issue,),
    )


def resolve_material_behavior(client_description, material_keys, material_catalog=None):
    """Resolve render mode and culling strictly from a declared material.

    Unsupported or missing material definitions remain visible through a
    conservative two-sided alpha-test fallback and emit structured MCB-W210
    metadata. Texture contents and model identifiers never participate.
    """
    key, declared_name = _declared_material(client_description, material_keys)
    if not declared_name:
        return _fallback_behavior(
            key,
            "",
            "No client material declaration could be resolved",
        )

    material_name = normalize_material_name(declared_name)
    if material_catalog is not None:
        definition = material_catalog.resolve(material_name)
        if definition is not None:
            fields = behavior_fields_from_definition(definition)
            return MaterialBehavior(
                key=key,
                name=declared_name,
                surface_mode=fields["surface_mode"],
                cutout_threshold=fields["cutout_threshold"],
                legacy_emission_mode=fields["legacy_emission_mode"],
                culling=fields["culling"],
                alpha_role=fields["alpha_role"],
                lighting=fields["lighting"],
                emission=fields["emission"],
                provenance=fields["provenance"],
                known=True,
                issues=fields["issues"],
            )
    canonical_name = material_name
    behavior = _MATERIAL_BEHAVIORS.get(canonical_name)
    if behavior is None and material_name == "entity_emissive_alpha_blend":
        # Existing packs use this hybrid name for a blended emissive pass.
        behavior = (
            "blend", 0.0, "inverse_alpha", "back", "coverage", "lit",
            "inverse_alpha",
        )
    if behavior is not None:
        return MaterialBehavior(
            key, declared_name, *behavior, ("canonical_bedrock_registry",), True
        )
    return _fallback_behavior(
        key,
        declared_name,
        f"Declared Bedrock material {declared_name!r} has no supported definition",
    )


def resolve_material_render_method(client_description, material_keys):
    """Backward-compatible render-mode projection of source behavior."""
    return resolve_material_behavior(
        client_description, material_keys
    ).render_method


def _render_pass_key(entry):
    """Identity of one authored render pass, excluding its controller id."""
    return (
        entry.get("geometry_identifier"),
        tuple(entry.get("texture_refs", [])),
        tuple(sorted(entry.get("hidden_parts", set()))),
        repr(entry.get("material", {})),
        repr(entry.get("material_rules", [])),
    )


def _deduplicate_active_render_passes(entries):
    unique = []
    seen = set()
    for entry in entries:
        if not entry.get("active", True):
            continue
        key = _render_pass_key(entry)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _collapse_rest_entries(entries, controller_ids, activation):
    """Keep rest pose to the default body mesh when prop controllers are unconditional."""
    visible_entries = _deduplicate_active_render_passes(entries)
    solid_unconditional = {
        entry.get("geometry_identifier")
        for entry in visible_entries
        if (
            activation.get(entry.get("controller"), "unknown") == "unconditional"
            and entry.get("material", {}).get("surface_mode") in {"opaque", "cutout"}
        )
    }
    if solid_unconditional:
        visible_entries = [
            entry for entry in visible_entries
            if not (
                entry.get("geometry_identifier") in solid_unconditional
                and activation.get(entry.get("controller"), "unknown") != "unconditional"
                and entry.get("material", {}).get("surface_mode") == "blend"
            )
        ]
    if len(visible_entries) <= 1:
        return visible_entries

    geometry_textures = [
        (entry.get("geometry_identifier"), tuple(entry.get("texture_refs", [])))
        for entry in visible_entries
    ]
    if len(set(geometry_textures)) < len(geometry_textures):
        return visible_entries

    # Preserve distinct geometry layers such as Breeze's wind shell.
    if any(entry.get("geometry_key") != "default" for entry in visible_entries):
        return visible_entries

    active_kinds = [activation.get(cid, "unknown") for cid in controller_ids]
    if (
        len(visible_entries) <= 2
        and len(active_kinds) == len(controller_ids)
        and all(kind == "unconditional" for kind in active_kinds)
    ):
        # Keep a base plus an unconditional texture/part overlay. Controllers
        # such as pig saddle intentionally reuse Geometry.default.
        unique = []
        seen = set()
        for entry in sorted(
            visible_entries,
            key=lambda item: "*" in item.get("hidden_parts", set()),
        ):
            key = (
                entry["geometry_identifier"],
                tuple(entry.get("texture_refs", [])),
                tuple(sorted(entry.get("hidden_parts", set()))),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        return unique

    default_entries = [
        entry for entry in visible_entries
        if entry.get("geometry_key") == "default"
    ]
    if default_entries:
        default_entries.sort(key=lambda item: "*" in item.get("hidden_parts", set()))
        if (
            len(default_entries) > 1
            and len({
                tuple(item.get("texture_refs", []))
                for item in default_entries
            }) > 1
        ):
            return default_entries
        return default_entries[:1]

    return visible_entries[:1]


def resolve_controller_entries(
    client_description,
    controller_ids,
    controller_lookup,
    variables=None,
    import_profile="rest",
    geometry_key_filter=None,
    query_context=None,
    material_catalog=None,
):
    """Return geometry/texture/visibility entries for import.

    ``import_profile``:
    - ``rest``: strict controller filter + collapse prop spam to one base mesh.
    - ``animation``: like ``all`` but optional ``geometry_key_filter`` from bone matching.
    - ``all``: every referenced Geometry.* token (cutscene/debug).
    """
    variables = variables or {}
    variables = _augment_preview_variables(variables, controller_ids, controller_lookup, query_context)
    rest = import_profile == "rest"
    expand_all = import_profile == "all"
    activation = _controller_activation(client_description, variables, query_context=query_context)
    geometries = client_description.get("geometry", {}) if isinstance(client_description, dict) else {}
    textures = client_description.get("textures", {}) if isinstance(client_description, dict) else {}
    geometries = geometries if isinstance(geometries, dict) else {}
    textures = textures if isinstance(textures, dict) else {}
    allowed_keys = set(geometry_key_filter or [])

    entries = []
    for controller_id in controller_ids:
        controller = controller_lookup(controller_id) or {}
        if expand_all:
            geometry_refs = _refs_from_expression(controller.get("geometry", ""), "Geometry") if controller else []
        else:
            geometry_refs = _resolve_resource_ref(
                controller.get("geometry", ""),
                "Geometry",
                variables,
                query_context,
                controller=controller,
                assume_query_false=False,
            ) if controller else []
        if not geometry_refs:
            if "default" in geometries:
                geometry_refs = ["default"]
            elif rest:
                geometry_refs = list(geometries.keys())[:1]
            else:
                geometry_refs = list(geometries.keys())

        if allowed_keys:
            geometry_refs = [key for key in geometry_refs if key in allowed_keys]
            if not geometry_refs:
                continue

        if expand_all:
            texture_refs = _refs_from_expression(controller.get("textures", []), "Texture") if controller else []
            texture_refs.extend(ref for ref in _array_texture_refs(controller) if ref not in texture_refs)
        else:
            texture_refs = _resolve_resource_ref(
                controller.get("textures", []),
                "Texture",
                variables,
                query_context,
                controller=controller,
                assume_query_false=False,
            ) if controller else []
            # Unknown array indexes must not silently select or composite
            # every entry. The authored default is chosen below.
        if not texture_refs:
            texture_refs = (
                ["default"] if "default" in textures
                else list(textures.keys())[:1]
            )

        raw_visibility = controller.get("part_visibility", []) if controller else []
        hidden_parts = hidden_parts_from_visibility(
            raw_visibility,
            variables,
            query_context=query_context,
            assume_query_false=False,
        ) if controller else set()
        unresolved_visibility = []
        for visibility_entry in raw_visibility if isinstance(raw_visibility, list) else []:
            if not isinstance(visibility_entry, dict):
                continue
            for part_pattern, expression in visibility_entry.items():
                if evaluate_visibility(expression, variables, query_context) is None:
                    unresolved_visibility.append(str(part_pattern))
        material_hints = _material_hints_from_controller(controller)
        material_behavior = resolve_material_behavior(
            client_description, material_hints, material_catalog
        )
        controller_metadata = _controller_material_metadata(
            controller, controller_id, variables, query_context
        )
        material_payload = material_behavior.as_dict()
        material_payload["issues"] = list(material_payload.get("issues", [])) + list(
            controller_metadata["issues"]
        )
        for field in (
            "ignore_lighting",
            "light_color_multiplier",
            "uv_offset",
            "uv_scale",
        ):
            material_payload[field] = controller_metadata[field]
        render_method = material_behavior.render_method
        material_rules = []
        raw_materials = controller.get("materials", []) if controller else []
        if isinstance(raw_materials, dict):
            raw_materials = [raw_materials]
        for material_entry in raw_materials if isinstance(raw_materials, list) else []:
            if not isinstance(material_entry, dict):
                continue
            for part_pattern, expression in material_entry.items():
                resolved_materials = _resolve_resource_ref(
                    expression,
                    "Material",
                    variables,
                    query_context,
                    controller=controller,
                    assume_query_false=False,
                )
                rule_behavior = resolve_material_behavior(
                    client_description, resolved_materials, material_catalog
                )
                rule_payload = rule_behavior.as_dict()
                rule_payload["issues"] = list(rule_payload.get("issues", [])) + list(
                    controller_metadata["issues"]
                )
                for field in (
                    "ignore_lighting", "light_color_multiplier", "uv_offset", "uv_scale"
                ):
                    rule_payload[field] = controller_metadata[field]
                material_rules.append({
                    "pattern": str(part_pattern),
                    "keys": resolved_materials,
                    "render_method": rule_behavior.render_method,
                    "material": rule_payload,
                })
        for geometry_key in geometry_refs:
            geometry_identifier = geometries.get(geometry_key)
            if not geometry_identifier:
                continue
            texture_keys = [key for key in texture_refs if key]
            if not texture_keys:
                texture_keys = [geometry_key if geometry_key in textures else "default"]
            resolved_texture_refs = [
                textures.get(texture_key)
                for texture_key in texture_keys
                if textures.get(texture_key)
            ]
            if not resolved_texture_refs:
                fallback_texture = textures.get(geometry_key) or textures.get("default") or ""
                if fallback_texture:
                    resolved_texture_refs = [fallback_texture]
            texture_key = texture_keys[0] if texture_keys else ""
            texture_ref = resolved_texture_refs[0] if resolved_texture_refs else ""
            all_parts_hidden = (
                "*" in hidden_parts
                and not any(str(part).startswith("!") for part in hidden_parts)
            )
            conditional_layer_unknown = bool(
                geometry_key != "default"
                and any(str(pattern).strip() == "*" for pattern in unresolved_visibility)
            )
            is_active = (
                activation.get(controller_id, "true") != "false"
                and not all_parts_hidden
                and not conditional_layer_unknown
            )
            entries.append({
                "controller": controller_id,
                "geometry_key": geometry_key,
                "geometry_identifier": geometry_identifier,
                "texture_key": texture_key,
                "texture_ref": texture_ref,
                "texture_keys": texture_keys,
                "texture_refs": resolved_texture_refs,
                "hidden_parts": hidden_parts,
                "unresolved_visibility": list(unresolved_visibility),
                "render_method": render_method,
                "material_rules": material_rules,
                "material": dict(material_payload),
                "cull_backfaces": material_behavior.cull_backfaces,
                "material_issues": list(material_payload["issues"]),
                "ignore_lighting": controller_metadata["ignore_lighting"],
                "light_color_multiplier": controller_metadata["light_color_multiplier"],
                "uv_offset": controller_metadata["uv_offset"],
                "uv_scale": controller_metadata["uv_scale"],
                "active": is_active,
            })

    collapse_variants = rest or (import_profile == "animation" and query_context is None)
    if collapse_variants and entries:
        entries = _collapse_rest_entries(entries, controller_ids, activation)
    elif not expand_all and entries:
        entries = _deduplicate_active_render_passes(entries)

    if not entries:
        fallback_items = list(geometries.items())
        if not expand_all and len(fallback_items) > 1:
            default_item = next(((k, v) for k, v in fallback_items if k == "default"), None)
            fallback_items = [default_item] if default_item else fallback_items[:1]
        default_behavior = resolve_material_behavior(
            client_description, [], material_catalog
        )
        for geometry_key, geometry_identifier in fallback_items:
            texture_ref = textures.get(geometry_key) or textures.get("default") or ""
            entries.append({
                "controller": "",
                "geometry_key": geometry_key,
                "geometry_identifier": geometry_identifier,
                "texture_key": geometry_key,
                "texture_ref": texture_ref,
                "texture_keys": [geometry_key],
                "texture_refs": [texture_ref] if texture_ref else [],
                "hidden_parts": set(),
                "render_method": default_behavior.render_method,
                "material": default_behavior.as_dict(),
                "cull_backfaces": default_behavior.cull_backfaces,
                "ignore_lighting": False,
                "light_color_multiplier": 1.0,
                "uv_offset": (0.0, 0.0),
                "uv_scale": (1.0, 1.0),
                "material_issues": list(default_behavior.issues),
                "material_rules": [],
                "active": True,
            })
    return entries
