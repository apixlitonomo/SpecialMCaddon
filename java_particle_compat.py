"""Safe, dependency-free compatibility helpers for Polytone particle JSON.

Polytone 1.21.11+ expressions are powered by MVEL. Blender must never execute
arbitrary Java/MVEL source from a resource pack, so this module intentionally
implements the numeric expression subset used by particle initializer fields
and reports scripts/statements as unsupported.
"""

from __future__ import annotations

import ast
import math
import operator
import random
import re
from dataclasses import dataclass, field


_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_FIELD_VARIABLES = {
    "x": "X", "y": "Y", "z": "Z",
    "dx": "DX", "dy": "DY", "dz": "DZ",
    "size": "SIZE", "red": "RED", "green": "GREEN", "blue": "BLUE",
    "alpha": "ALPHA", "roll": "ROLL", "custom": "CUSTOM",
    "lifetime": "LIFE", "life": "LIFE", "friction": "FRICTION",
    "hitbox_size": "HITBOX_SIZE", "speed": "SPEED",
}
_CONTEXT_ALIASES = {
    "o.age": "AGE", "object.age": "AGE",
    "o.life": "LIFE", "object.life": "LIFE",
    "o.size": "SIZE", "object.size": "SIZE",
    "o.red": "RED", "object.red": "RED",
    "o.green": "GREEN", "object.green": "GREEN",
    "o.blue": "BLUE", "object.blue": "BLUE",
    "o.alpha": "ALPHA", "object.alpha": "ALPHA",
    "o.roll": "ROLL", "object.roll": "ROLL",
    "o.xd": "DX", "object.xd": "DX",
    "o.yd": "DY", "object.yd": "DY",
    "o.zd": "DZ", "object.zd": "DZ",
    "global.time": "GAMETIME", "g.time": "GAMETIME",
}


@dataclass
class PolytoneApproximation:
    values: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    render_type: str = "opaque"
    rotation_mode: object = "look_at_xyz"
    random_sprite: bool = False
    has_physics: bool = True

    @property
    def supported(self):
        return not self.warnings


class UnsupportedMVEL(ValueError):
    pass


def _translate_ternary(expression):
    """Translate one non-nested MVEL ``condition ? yes : no`` expression."""
    depth = 0
    question = -1
    colon = -1
    for index, char in enumerate(expression):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "?" and depth == 0 and question < 0:
            question = index
        elif char == ":" and depth == 0 and question >= 0:
            colon = index
            break
    if question < 0:
        return expression
    if colon < 0:
        raise UnsupportedMVEL("incomplete ternary expression")
    condition = expression[:question].strip()
    yes = expression[question + 1:colon].strip()
    no = expression[colon + 1:].strip()
    return f"({yes} if {condition} else {no})"


def _translate(expression):
    value = str(expression or "").strip()
    if not value:
        raise UnsupportedMVEL("empty expression")
    if any(token in value for token in (";", "{", "}")):
        raise UnsupportedMVEL("statement-style MVEL is not executed")
    value = value.replace("Math.", "").replace("math.", "")
    value = value.replace("&&", " and ").replace("||", " or ")
    value = re.sub(r"(?<![=!<>])!(?!=)", " not ", value)
    value = re.sub(r"\btrue\b", "True", value, flags=re.IGNORECASE)
    value = re.sub(r"\bfalse\b", "False", value, flags=re.IGNORECASE)
    value = re.sub(r"\bnull\b", "None", value, flags=re.IGNORECASE)
    for source, target in sorted(
        _CONTEXT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        value = re.sub(
            rf"(?<![\w.]){re.escape(source)}(?:\(\))?",
            target,
            value,
            flags=re.IGNORECASE,
        )
    return _translate_ternary(value)


def _functions(rng):
    return {
        "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "atan2": math.atan2, "sqrt": math.sqrt, "abs": abs,
        "log": math.log, "exp": math.exp, "pow": pow,
        "floor": math.floor, "ceil": math.ceil, "round": round,
        "min": min, "max": max,
        "clamp": lambda value, low, high: max(low, min(high, value)),
        "radians": math.radians, "degrees": math.degrees,
        "lerp": lambda a, b, t: a + (b - a) * t,
        "rand": lambda *args: (
            rng.random() if not args else (
                rng.random() * args[0] if len(args) == 1
                else args[0] + rng.random() * (args[1] - args[0])
            )
        ),
        "gaussian": lambda *args: (
            rng.gauss(0.0, 1.0) if not args else (
                rng.gauss(args[0], args[1]) if len(args) > 1
                else rng.gauss(0.0, args[0])
            )
        ),
    }


def _eval_node(node, variables, functions):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables, functions)
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (int, float, bool, type(None))
    ):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in variables:
            return variables[node.id]
        raise UnsupportedMVEL(f"unknown variable {node.id}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        return _BINARY[type(node.op)](
            _eval_node(node.left, variables, functions),
            _eval_node(node.right, variables, functions),
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval_node(node.operand, variables, functions))
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        values = [_eval_node(item, variables, functions) for item in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, variables, functions)
        for operation, comparator in zip(node.ops, node.comparators):
            function = _COMPARE.get(type(operation))
            if function is None:
                raise UnsupportedMVEL("unsupported comparison")
            right = _eval_node(comparator, variables, functions)
            if not function(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        branch = node.body if _eval_node(node.test, variables, functions) else node.orelse
        return _eval_node(branch, variables, functions)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in functions
        and not node.keywords
    ):
        return functions[node.func.id](
            *[_eval_node(argument, variables, functions) for argument in node.args]
        )
    raise UnsupportedMVEL(f"unsupported syntax {type(node).__name__}")


def evaluate_mvel_numeric(expression, variables=None, rng=None):
    """Evaluate Polytone's safe numeric subset without using Python ``eval``."""
    translated = _translate(expression)
    try:
        tree = ast.parse(translated, mode="eval")
        scope = _default_variables()
        scope.update({str(key): value for key, value in (variables or {}).items()})
        value = _eval_node(
            tree,
            scope,
            _functions(rng or random.Random(0)),
        )
    except UnsupportedMVEL:
        raise
    except Exception as exc:
        raise UnsupportedMVEL(str(exc) or "expression failed") from exc
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise UnsupportedMVEL("expression did not return a finite number")
    return float(value)


def _default_variables():
    return {
        "X": 0.0, "Y": 0.0, "Z": 0.0,
        "DX": 0.0, "DY": 0.0, "DZ": 0.0,
        "SIZE": 1.0, "RED": 1.0, "GREEN": 1.0, "BLUE": 1.0,
        "ALPHA": 1.0, "ROLL": 0.0, "CUSTOM": 0.0, "AGE": 0.0,
        "LIFE": 20.0, "FRICTION": 0.98, "HITBOX_SIZE": 0.1,
        "GAMETIME": 0.0, "SPEED": 0.0,
        "PI": math.pi, "TAU": math.tau, "E": math.e,
        "pi": math.pi, "tau": math.tau, "e": math.e,
    }


def approximate_polytone_particle(definition, modifier=None, seed=0):
    """Resolve importer settings from safe initializer/modifier expressions."""
    definition = definition if isinstance(definition, dict) else {}
    modifier = modifier if isinstance(modifier, dict) else {}
    result = PolytoneApproximation(
        render_type=str(definition.get("render_type", "opaque") or "opaque"),
        rotation_mode=definition.get("rotation_mode", "look_at_xyz"),
        random_sprite=bool(definition.get("random_sprite", False)),
        has_physics=bool(definition.get("has_physics", True)),
    )
    variables = _default_variables()
    rng = random.Random(seed)

    initializer = definition.get("initializer", {})
    if not isinstance(initializer, dict):
        if initializer:
            result.warnings.append("Polytone initializer script requires full MVEL")
        initializer = {}

    expressions = dict(initializer)
    for key in (
        "red", "green", "blue", "alpha", "size", "life", "lifetime", "speed"
    ):
        if key in modifier:
            expressions[key] = modifier[key]

    for field_name, expression in expressions.items():
        variable = _FIELD_VARIABLES.get(str(field_name).casefold())
        if not variable:
            continue
        try:
            value = evaluate_mvel_numeric(expression, variables, rng)
            variables[variable] = value
            result.values[str(field_name).casefold()] = value
        except UnsupportedMVEL as exc:
            result.warnings.append(f"{field_name}: {exc}")

    if definition.get("ticker"):
        result.warnings.append(
            "ticker MVEL is preserved as metadata but not executed by Blender"
        )
    if definition.get("particle_emitters"):
        result.warnings.append("nested Polytone particle emitters are not baked")
    if definition.get("sound_emitters"):
        result.warnings.append("Polytone sound emitters are not imported")
    return result
