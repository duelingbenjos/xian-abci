"""Recover authored contract source from legacy (pre-VM) Xian chain state.

The legacy CPython engine stored a *transformed* module under
``<contract>.__code__`` rather than the authored source. The transform was
mechanical (see the legacy ``contracting.compilation.compiler``):

- ``@export`` became ``@__export('<contract>')``
- the ``@construct`` function was renamed to ``____`` and lost its decorator
- every non-exported function ``f`` was renamed to ``__f``
- every ORM object ``x = Hash(...)`` became ``__x = Hash(..., contract='<contract>',
  name='x')``, wherever it was assigned, and every reference was renamed
- float literals became ``decimal('<literal>')``

``recover_legacy_source`` inverts those steps on the AST so the result can be
recompiled into ``xian_vm_v1`` artifacts. Float literals stay as
``decimal('...')`` calls because that is what the legacy chain executed.

Compatibility rewrites are separate and explicit: they change source text that
the legacy engine accepted but the current compiler rejects, and every applied
rewrite is reported to the caller.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field

LEGACY_ORM_CLASS_NAMES = frozenset(
    {"Variable", "Hash", "ForeignVariable", "ForeignHash", "LogEvent"}
)
LEGACY_PRIVATE_PREFIX = "__"
LEGACY_CONSTRUCTOR_NAME = "____"
LEGACY_EXPORT_DECORATOR = "__export"
RECOVERED_CONSTRUCTOR_NAME = "seed"


class LegacySourceError(ValueError):
    pass


def _is_legacy_orm_assignment(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith(LEGACY_PRIVATE_PREFIX)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in LEGACY_ORM_CLASS_NAMES
    )


def _collect_legacy_renames(tree: ast.Module) -> dict[str, str]:
    renames: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.FunctionDef)
            and node.name != LEGACY_CONSTRUCTOR_NAME
            and node.name.startswith(LEGACY_PRIVATE_PREFIX)
            and not node.decorator_list
        ):
            renames[node.name] = node.name[len(LEGACY_PRIVATE_PREFIX) :]
    # The legacy compiler privatized ORM objects in any scope, including ORM
    # handles created inside function bodies.
    for node in ast.walk(tree):
        if _is_legacy_orm_assignment(node):
            target = node.targets[0].id
            renames[target] = target[len(LEGACY_PRIVATE_PREFIX) :]
    return renames


def _unrenamed_identifiers(tree: ast.Module, renames: dict[str, str]) -> set[str]:
    """Identifiers the legacy transform left alone (parameters, unprivatized names).

    If a recovered name equals one of these, restoring it would change which
    binding a reference resolves to, so recovery must refuse.
    """
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.Name) and node.id not in renames:
            identifiers.add(node.id)
        elif isinstance(node, ast.FunctionDef) and node.name not in renames:
            identifiers.add(node.name)
    return identifiers


class _LegacyRestorer(ast.NodeTransformer):
    def __init__(self, contract_name: str, renames: dict[str, str]):
        self.contract_name = contract_name
        self.renames = renames
        self.constructors = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        self.generic_visit(node)
        if node.name == LEGACY_CONSTRUCTOR_NAME:
            self.constructors += 1
            node.name = RECOVERED_CONSTRUCTOR_NAME
            node.decorator_list = [ast.Name(id="construct", ctx=ast.Load())]
            return node
        if node.decorator_list:
            if len(node.decorator_list) != 1:
                raise LegacySourceError(
                    f"{self.contract_name}: function {node.name!r} has multiple decorators"
                )
            decorator = node.decorator_list[0]
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == LEGACY_EXPORT_DECORATOR
            ):
                raise LegacySourceError(
                    f"{self.contract_name}: function {node.name!r} has an unexpected "
                    f"decorator {ast.unparse(decorator)!r}"
                )
            node.decorator_list = [ast.Name(id="export", ctx=ast.Load())]
            return node
        node.name = self.renames.get(node.name, node.name)
        return node

    def visit_Call(self, node: ast.Call) -> ast.Call:
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in LEGACY_ORM_CLASS_NAMES:
            node.keywords = [
                keyword for keyword in node.keywords if keyword.arg not in {"contract", "name"}
            ]
        return node

    def visit_Name(self, node: ast.Name) -> ast.Name:
        renamed = self.renames.get(node.id)
        if renamed is not None:
            node.id = renamed
        return node


@dataclass(frozen=True)
class RecoveredSource:
    source: str
    # legacy binding (e.g. ``__burn``) -> renamed authored binding, only for
    # names that collided and had to diverge from the original authored name
    collision_renames: dict[str, str] = field(default_factory=dict)
    # ORM storage variable renames the caller must mirror in contract state
    storage_renames: dict[str, str] = field(default_factory=dict)


STORAGE_ORM_CLASS_NAMES = frozenset({"Variable", "Hash"})


def _legacy_binding_kinds(tree: ast.Module) -> dict[str, str]:
    kinds: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            kinds[node.name] = "function"
    for node in ast.walk(tree):
        if _is_legacy_orm_assignment(node):
            storage = node.value.func.id in STORAGE_ORM_CLASS_NAMES and node in tree.body
            kinds[node.targets[0].id] = "storage" if storage else "orm_handle"
    return kinds


def _fresh_name(base: str, suffix: str, taken: set[str]) -> str:
    candidate = f"{base}_{suffix}"
    counter = 2
    while candidate in taken:
        candidate = f"{base}_{suffix}{counter}"
        counter += 1
    return candidate


def recover_legacy_source(
    contract_name: str,
    legacy_code: str,
    *,
    resolve_collisions: bool = False,
) -> RecoveredSource:
    """Invert the legacy compiler transform for ``contract_name``.

    A recovered name can collide with an identifier the legacy transform left
    alone (a parameter or exported function of the same name). Restoring it
    would silently rebind references, so recovery refuses unless
    ``resolve_collisions`` is set. Then the privatized binding gets a fresh
    name instead (``<name>_private`` for functions, ``<name>_store`` for ORM
    objects) and storage renames are returned for the caller to apply.
    """
    try:
        tree = ast.parse(legacy_code)
    except SyntaxError as exc:
        raise LegacySourceError(f"{contract_name}: legacy code does not parse: {exc}") from exc

    renames = _collect_legacy_renames(tree)
    untouched = _unrenamed_identifiers(tree, renames)
    collisions = sorted(set(renames.values()) & untouched)
    collision_renames: dict[str, str] = {}
    storage_renames: dict[str, str] = {}
    if collisions:
        if not resolve_collisions:
            raise LegacySourceError(
                f"{contract_name}: recovered names collide with privatized names: {collisions}"
            )
        kinds = _legacy_binding_kinds(tree)
        taken = untouched | set(renames.values())
        for legacy_name, recovered in sorted(renames.items()):
            if recovered not in collisions:
                continue
            kind = kinds.get(legacy_name, "orm_handle")
            fresh = _fresh_name(recovered, "private" if kind == "function" else "store", taken)
            taken.add(fresh)
            renames[legacy_name] = fresh
            collision_renames[legacy_name] = fresh
            if kind == "storage":
                storage_renames[recovered] = fresh

    restorer = _LegacyRestorer(contract_name, renames)
    tree = restorer.visit(tree)
    if restorer.constructors > 1:
        raise LegacySourceError(f"{contract_name}: legacy code has multiple constructors")
    ast.fix_missing_locations(tree)
    return RecoveredSource(
        source=ast.unparse(tree) + "\n",
        collision_renames=collision_renames,
        storage_renames=storage_renames,
    )


# --- Compatibility rewrites -------------------------------------------------


def _contains_string_constant(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Constant) and isinstance(child.value, str) for child in ast.walk(node)
    )


def _plain_formatted_value(value: ast.AST) -> bool:
    return (
        isinstance(value, ast.FormattedValue)
        and value.conversion == -1
        and value.format_spec is None
    )


class _FStringToConcat(ast.NodeTransformer):
    """Rewrite f-strings whose fields contain string literals into concatenation.

    ``xian_vm_v1``'s parser rejects a string literal inside an f-string
    replacement field (``f"{d[k, 'x']}"``). For fields with no conversion and no
    format spec, ``f"a{x}b"`` evaluates exactly like ``'a' + str(x) + 'b'``:
    the same values, left to right, with the same text. F-strings that need any
    other feature are left untouched so compilation fails loudly instead.
    """

    def __init__(self):
        self.rewritten = 0

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        fields = [value for value in node.values if isinstance(value, ast.FormattedValue)]
        if not any(_contains_string_constant(field.value) for field in fields):
            return node
        if not all(_plain_formatted_value(field) for field in fields):
            return node

        parts: list[ast.expr] = []
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                parts.append(
                    ast.Call(
                        func=ast.Name(id="str", ctx=ast.Load()),
                        args=[value.value],
                        keywords=[],
                    )
                )
            else:
                parts.append(value)
        if not parts:
            return node
        if not (isinstance(parts[0], ast.Constant) and isinstance(parts[0].value, str)):
            parts.insert(0, ast.Constant(value=""))

        expression = parts[0]
        for part in parts[1:]:
            expression = ast.BinOp(left=expression, op=ast.Add(), right=part)
        self.rewritten += 1
        return ast.copy_location(expression, node)


def _rewrite_fstring_string_literals(source: str) -> tuple[str, int]:
    tree = ast.parse(source)
    transformer = _FStringToConcat()
    tree = transformer.visit(tree)
    if transformer.rewritten == 0:
        return source, 0
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n", transformer.rewritten


@dataclass(frozen=True)
class CompatRewrite:
    name: str
    description: str
    apply: Callable[[str], tuple[str, int]]


COMPAT_REWRITES: dict[str, CompatRewrite] = {
    rewrite.name: rewrite
    for rewrite in (
        CompatRewrite(
            name="fstring_string_literals",
            description=(
                "f-strings whose replacement fields contain string literals are "
                "rewritten to equivalent str() concatenation"
            ),
            apply=_rewrite_fstring_string_literals,
        ),
    )
}


def apply_compat_rewrites(
    source: str,
    *,
    allowed: frozenset[str] | set[str],
) -> tuple[str, dict[str, int]]:
    """Apply the allowed compatibility rewrites; return new source and per-rewrite counts."""
    unknown = set(allowed) - set(COMPAT_REWRITES)
    if unknown:
        raise LegacySourceError(f"unknown compatibility rewrites: {sorted(unknown)}")
    applied: dict[str, int] = {}
    for name in sorted(allowed):
        source, count = COMPAT_REWRITES[name].apply(source)
        if count:
            applied[name] = count
    return source, applied
