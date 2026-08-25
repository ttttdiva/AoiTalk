#!/usr/bin/env python3
"""Check that an Enterprise artifact has a closed set of internal imports."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
import tokenize
from typing import Iterable, Sequence


# These modules are deliberately absent from Enterprise artifacts and are only
# optional when imported inside a try block that catches the real builtin
# ImportError. Matching is intentionally exact.
IMPORT_ERROR_ALLOWLIST = frozenset(
    {
        "src.api.comfyui_routes",
        "src.tools.hydrus_browser",
    }
)


@dataclass(frozen=True)
class DynamicRegistryRule:
    variable: str | None = None
    value_part: str = "whole"
    relative_package: str | None = None
    plain_values_are_relative: bool = False
    fixed_modules: tuple[str, ...] = ()


# Non-literal imports are rejected unless the exact call scope has a registry
# whose literal candidates can be enumerated from the artifact itself.
DYNAMIC_IMPORT_REGISTRIES = {
    ("src.agents", "__getattr__"): DynamicRegistryRule(
        variable="_AGENT_MODULES",
        relative_package="src.agents",
        plain_values_are_relative=True,
    ),
    ("src.audio", "__getattr__"): DynamicRegistryRule(
        variable="_LAZY_IMPORTS", value_part="tuple_first", relative_package="src.audio"
    ),
    ("src.bot", "__getattr__"): DynamicRegistryRule(
        variable="_LAZY_IMPORTS", value_part="tuple_first", relative_package="src.bot"
    ),
    ("src.mcp_server_entry", "main"): DynamicRegistryRule(variable="SERVER_MODULES"),
    (
        "src.audio.manager",
        "SpeechRecognitionManager._load_engine_class",
    ): DynamicRegistryRule(
        variable="SpeechRecognitionManager._engine_registry",
        value_part="without_last_component",
        relative_package="src.audio",
    ),
    ("src.memory", "__getattr__"): DynamicRegistryRule(
        variable="_EXPORTS", value_part="tuple_first", relative_package="src.memory"
    ),
    ("src.tts", "__getattr__"): DynamicRegistryRule(
        variable="_LAZY_IMPORTS", value_part="tuple_first", relative_package="src.tts"
    ),
    ("src.tools.file_explorer", "__getattr__"): DynamicRegistryRule(
        fixed_modules=("src.tools.file_explorer.file_explorer_tools",)
    ),
    (
        "src.tts.engines.aivoice_engine",
        "AIVoiceEngine._try_api_connection",
    ): DynamicRegistryRule(variable="AIVoiceEngine._try_api_connection.possible_imports"),
}


@dataclass(frozen=True, order=True)
class CheckError:
    path: str
    line: int
    detail: str

    def render(self) -> str:
        return f"{self.path}:{self.line} -> {self.detail}"


@dataclass(frozen=True)
class ModuleIndex:
    modules: frozenset[str]
    package_initializers: frozenset[str]
    namespace_prefixes: frozenset[str]
    top_level_names: frozenset[str]

    @property
    def importable_names(self) -> frozenset[str]:
        return self.modules | self.package_initializers | self.namespace_prefixes

    def canonicalize_absolute(self, name: str) -> str | None:
        if name == "src" or name.startswith("src."):
            return name
        first, separator, remainder = name.partition(".")
        if first not in self.top_level_names:
            return None
        return f"src.{first}{separator}{remainder}"

    def contains_required(self, source: ModuleIndex, name: str) -> bool:
        if name in source.package_initializers:
            return name in self.package_initializers
        if name in source.modules:
            return name in self.modules
        if name in source.namespace_prefixes:
            return name in self.importable_names
        return name in self.importable_names


@dataclass
class ScanResult:
    files: list[Path] = field(default_factory=list)
    errors: set[CheckError] = field(default_factory=set)
    valid: bool = True


def _path_label(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return str(path)


def _lstat_kind(path: Path) -> tuple[bool, bool]:
    metadata = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
    return stat.S_ISLNK(metadata.st_mode) or is_reparse, stat.S_ISDIR(metadata.st_mode)


def _scan_python_files(root: Path, role: str) -> ScanResult:
    result = ScanResult()

    try:
        root_link, root_is_dir = _lstat_kind(root)
    except OSError as error:
        result.errors.add(CheckError(".", 1, f"{role} root lstat error: {error}"))
        result.valid = False
        return result
    if root_link or not root_is_dir:
        detail = "must be a regular directory (not a symlink/reparse point)"
        result.errors.add(CheckError(".", 1, f"{role} root {detail}"))
        result.valid = False
        return result

    # A root below a junction can escape the tree even when the root itself is
    # ordinary. Inspect existing ancestors without resolving through them.
    for ancestor in root.absolute().parents:
        try:
            ancestor_link, _ = _lstat_kind(ancestor)
        except OSError as error:
            result.errors.add(
                CheckError(".", 1, f"{role} root ancestor lstat error: {error}")
            )
            result.valid = False
            return result
        if ancestor_link:
            result.errors.add(
                CheckError(
                    ".",
                    1,
                    f"{role} root ancestor is a symlink/reparse point: {ancestor}",
                )
            )
            result.valid = False
            return result

    src_root = root / "src"
    try:
        src_link, src_is_dir = _lstat_kind(src_root)
    except OSError as error:
        result.errors.add(CheckError("src", 1, f"{role} src lstat error: {error}"))
        result.valid = False
        return result
    if src_link or not src_is_dir:
        detail = "must be a regular directory (not a symlink/reparse point)"
        result.errors.add(CheckError("src", 1, f"{role} src {detail}"))
        result.valid = False
        return result

    def on_walk_error(error: OSError) -> None:
        filename = Path(error.filename) if error.filename else src_root
        result.errors.add(
            CheckError(
                _path_label(root, filename),
                1,
                f"{role} src walk access error: {error}",
            )
        )
        result.valid = False

    try:
        walker = os.walk(src_root, topdown=True, followlinks=False, onerror=on_walk_error)
        for directory, directory_names, file_names in walker:
            directory_path = Path(directory)
            kept_directories: list[str] = []
            for name in sorted(directory_names):
                path = directory_path / name
                try:
                    is_link, is_dir = _lstat_kind(path)
                except OSError as error:
                    result.errors.add(
                        CheckError(
                            _path_label(root, path),
                            1,
                            f"{role} src lstat error: {error}",
                        )
                    )
                    result.valid = False
                    continue
                if is_link:
                    result.errors.add(
                        CheckError(
                            _path_label(root, path),
                            1,
                            f"{role} src contains a symlink/reparse point",
                        )
                    )
                    result.valid = False
                elif not is_dir:
                    result.errors.add(
                        CheckError(
                            _path_label(root, path),
                            1,
                            f"{role} src directory entry is not a directory",
                        )
                    )
                    result.valid = False
                else:
                    kept_directories.append(name)
            directory_names[:] = kept_directories

            for name in sorted(file_names):
                path = directory_path / name
                try:
                    is_link, _ = _lstat_kind(path)
                    is_regular = stat.S_ISREG(path.lstat().st_mode)
                except OSError as error:
                    result.errors.add(
                        CheckError(
                            _path_label(root, path),
                            1,
                            f"{role} src lstat error: {error}",
                        )
                    )
                    result.valid = False
                    continue
                if is_link:
                    result.errors.add(
                        CheckError(
                            _path_label(root, path),
                            1,
                            f"{role} src contains a symlink/reparse point",
                        )
                    )
                    result.valid = False
                elif not is_regular:
                    result.errors.add(
                        CheckError(
                            _path_label(root, path),
                            1,
                            f"{role} src file entry is not a regular file",
                        )
                    )
                    result.valid = False
                elif name.endswith(".py"):
                    result.files.append(path)
    except OSError as error:
        on_walk_error(error)

    return result


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def build_module_index(root: Path, files: Iterable[Path]) -> ModuleIndex:
    modules: set[str] = set()
    package_initializers: set[str] = set()
    prefixes: set[str] = set()
    top_level_names: set[str] = set()
    for path in files:
        module = _module_name(root, path)
        if path.name == "__init__.py":
            package_initializers.add(module)
        else:
            modules.add(module)
        parts = module.split(".")
        if len(parts) > 1:
            top_level_names.add(parts[1])
        for end in range(1, len(parts)):
            prefixes.add(".".join(parts[:end]))
    namespace_prefixes = prefixes - package_initializers - modules
    return ModuleIndex(
        frozenset(modules),
        frozenset(package_initializers),
        frozenset(namespace_prefixes),
        frozenset(top_level_names),
    )


def _represented_package_prefixes(importable_names: Iterable[str]) -> frozenset[str]:
    represented: set[str] = set()
    for name in importable_names:
        parts = name.split(".")
        for end in range(1, len(parts) + 1):
            represented.add(".".join(parts[:end]))
    return frozenset(represented)


def _resolve_relative(package: str, level: int, module: str | None) -> str | None:
    package_parts = package.split(".") if package else []
    remove_count = level - 1
    if level < 1 or remove_count >= len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - remove_count]
    if module:
        base_parts.extend(module.split("."))
    return ".".join(base_parts) or None


@dataclass(frozen=True)
class BindingEvent:
    position: tuple[int, int, int]
    import_target: str | None
    may_bind: bool = False


@dataclass(frozen=True)
class BindingResolution:
    state: str
    import_target: str | None = None
    possible_import_targets: frozenset[str] = frozenset()
    scope: Scope | None = None
    binding_position: tuple[int, int] | None = None


@dataclass(eq=False)
class Scope:
    kind: str
    parent: Scope | None
    bindings: dict[str, list[BindingEvent]] = field(default_factory=dict)
    local_names: set[str] = field(default_factory=set)
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)
    wildcard_imports: list[BindingEvent] = field(default_factory=list)

    def bind_import(
        self,
        name: str,
        target: str,
        position: tuple[int, int, int],
        *,
        may_bind: bool = False,
    ) -> None:
        self.local_names.add(name)
        self.bindings.setdefault(name, []).append(
            BindingEvent(position, target, may_bind)
        )

    def bind_shadow(
        self,
        name: str,
        position: tuple[int, int, int],
        *,
        may_bind: bool = False,
    ) -> None:
        self.local_names.add(name)
        self.bindings.setdefault(name, []).append(
            BindingEvent(position, None, may_bind)
        )


def _bound_names(target: ast.AST) -> Iterable[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            yield from _bound_names(item)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


def _pattern_bound_names(pattern: ast.AST) -> Iterable[str]:
    if isinstance(pattern, ast.MatchAs):
        if pattern.pattern is not None:
            yield from _pattern_bound_names(pattern.pattern)
        if pattern.name is not None:
            yield pattern.name
    elif isinstance(pattern, ast.MatchStar):
        if pattern.name is not None:
            yield pattern.name
    elif isinstance(pattern, ast.MatchMapping):
        for child in pattern.patterns:
            yield from _pattern_bound_names(child)
        if pattern.rest is not None:
            yield pattern.rest
    elif isinstance(pattern, ast.MatchSequence):
        for child in pattern.patterns:
            yield from _pattern_bound_names(child)
    elif isinstance(pattern, ast.MatchClass):
        for child in (*pattern.patterns, *pattern.kwd_patterns):
            yield from _pattern_bound_names(child)
    elif isinstance(pattern, ast.MatchOr):
        for child in pattern.patterns:
            yield from _pattern_bound_names(child)


class ScopeBuilder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.root = Scope("module", None)
        self.current = self.root
        self.scopes: dict[ast.AST, Scope] = {}
        self._event_sequence = 0
        self._may_bind_depth = 0

    @property
    def _may_bind(self) -> bool:
        return self._may_bind_depth > 0 and self.current.kind in {"module", "class"}

    def _visit_may_bind_statements(self, statements: Iterable[ast.AST]) -> None:
        self._may_bind_depth += 1
        try:
            for statement in statements:
                self.visit(statement)
        finally:
            self._may_bind_depth -= 1

    def _position_after(self, node: ast.AST) -> tuple[int, int, int]:
        self._event_sequence += 1
        return (
            getattr(node, "end_lineno", getattr(node, "lineno", 0)),
            getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
            self._event_sequence,
        )

    def _shadow_target(self, target: ast.AST, event_node: ast.AST | None = None) -> None:
        position = self._position_after(event_node or target)
        for name in _bound_names(target):
            if name not in self.current.globals and name not in self.current.nonlocals:
                self.current.bind_shadow(name, position, may_bind=self._may_bind)

    def _enter(self, node: ast.AST, kind: str, arguments: ast.arguments | None = None) -> None:
        parent = self.current
        child = Scope(kind, parent)
        self.scopes[node] = child
        self.current = child
        previous_may_bind_depth = self._may_bind_depth
        self._may_bind_depth = 0
        if arguments is not None:
            for argument in (
                list(arguments.posonlyargs)
                + list(arguments.args)
                + list(arguments.kwonlyargs)
            ):
                child.bind_shadow(argument.arg, self._position_after(argument))
            if arguments.vararg:
                child.bind_shadow(
                    arguments.vararg.arg, self._position_after(arguments.vararg)
                )
            if arguments.kwarg:
                child.bind_shadow(
                    arguments.kwarg.arg, self._position_after(arguments.kwarg)
                )
        body = getattr(node, "body", ())
        if isinstance(body, list):
            for statement in body:
                self.visit(statement)
        else:
            self.visit(body)
        self._may_bind_depth = previous_may_bind_depth
        self.current = parent

    def visit_Global(self, node: ast.Global) -> None:
        self.current.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.current.nonlocals.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        position = self._position_after(node)
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            target = alias.name if alias.asname else alias.name.split(".")[0]
            self.current.bind_import(name, target, position, may_bind=self._may_bind)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        position = self._position_after(node)
        if node.level:
            for alias in node.names:
                if alias.name == "*":
                    self.current.wildcard_imports.append(
                        BindingEvent(position, None, True)
                    )
                else:
                    self.current.bind_shadow(
                        alias.asname or alias.name,
                        position,
                        may_bind=self._may_bind,
                    )
            return
        for alias in node.names:
            if alias.name == "*":
                self.current.wildcard_imports.append(BindingEvent(position, None, True))
            elif node.module:
                self.current.bind_import(
                    alias.asname or alias.name,
                    f"{node.module}.{alias.name}",
                    position,
                    may_bind=self._may_bind,
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._shadow_target(target, node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value:
            self.visit(node.value)
        self._shadow_target(node.target, node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._shadow_target(node.target, node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        target_scope = self.current
        while target_scope.kind == "comprehension" and target_scope.parent is not None:
            target_scope = target_scope.parent
        position = self._position_after(node)
        for name in _bound_names(node.target):
            target_scope.bind_shadow(
                name,
                position,
                may_bind=self._may_bind and target_scope is self.current,
            )
        self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._may_bind_depth += 1
        try:
            self._shadow_target(node.target, node.target)
            for statement in (*node.body, *node.orelse):
                self.visit(statement)
        finally:
            self._may_bind_depth -= 1

    visit_AsyncFor = visit_For

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._visit_may_bind_statements((*node.body, *node.orelse))

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._visit_may_bind_statements((*node.body, *node.orelse))

    def visit_Try(self, node: ast.Try) -> None:
        children: list[ast.AST] = [*node.body, *node.orelse, *node.finalbody]
        children.extend(node.handlers)
        self._visit_may_bind_statements(children)

    visit_TryStar = visit_Try

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self._shadow_target(item.optional_vars, item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.current.bind_shadow(
                node.name,
                self._position_after(node.type or node),
                may_bind=self._may_bind,
            )
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._shadow_target(target, node)

    def visit_TypeAlias(self, node: ast.AST) -> None:
        target = getattr(node, "name", None)
        value = getattr(node, "value", None)
        if isinstance(value, ast.AST):
            self.visit(value)
        if isinstance(target, ast.AST):
            self._shadow_target(target, node)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        self._may_bind_depth += 1
        try:
            for case in node.cases:
                self.visit(case.pattern)
                position = self._position_after(case.pattern)
                for name in _pattern_bound_names(case.pattern):
                    if name not in self.current.globals and name not in self.current.nonlocals:
                        self.current.bind_shadow(
                            name, position, may_bind=self._may_bind
                        )
                if case.guard is not None:
                    self.visit(case.guard)
                for statement in case.body:
                    self.visit(statement)
        finally:
            self._may_bind_depth -= 1

    def _visit_function_outer_expressions(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default:
                self.visit(default)
        if node.returns:
            self.visit(node.returns)
        for argument in (
            list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
        ):
            if argument.annotation:
                self.visit(argument.annotation)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_outer_expressions(node)
        self.current.bind_shadow(
            node.name, self._position_after(node), may_bind=self._may_bind
        )
        self._enter(node, "function", node.args)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._enter(node, "function", node.args)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self.current.bind_shadow(
            node.name, self._position_after(node), may_bind=self._may_bind
        )
        self._enter(node, "class")

    def _visit_comprehension(self, node: ast.AST) -> None:
        generators = node.generators  # type: ignore[attr-defined]
        if not generators:
            return
        # Python evaluates the first iterable in the enclosing scope. Targets,
        # filters, later iterables, and the result expression use the implicit
        # comprehension scope.
        self.visit(generators[0].iter)
        parent = self.current
        child = Scope("comprehension", parent)
        self.scopes[node] = child
        self.current = child
        for index, generator in enumerate(generators):
            if index:
                self.visit(generator.iter)
            self._shadow_target(generator.target, generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)  # type: ignore[attr-defined]
        self.current = parent

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension


class BindingResolver:
    def __init__(self, root: Scope) -> None:
        self.root = root

    def _next_scope(self, scope: Scope) -> Scope | None:
        parent = scope.parent
        if scope.kind in {"function", "comprehension"} and parent and parent.kind == "class":
            return parent.parent
        return parent

    @staticmethod
    def _position(node: ast.AST) -> tuple[int, int, int]:
        return (
            getattr(node, "lineno", 0),
            getattr(node, "col_offset", 0),
            1 << 30,
        )

    def resolve(self, name: str, scope: Scope, node: ast.AST) -> BindingResolution:
        return self._resolve(name, scope, self._position(node))

    def _resolve(
        self,
        name: str,
        current: Scope | None,
        position: tuple[int, int, int],
    ) -> BindingResolution:
        if current is None:
            return BindingResolution("unbound")
        if name in current.globals and current is not self.root:
            if current.kind == "function":
                position = (1 << 30, 1 << 30, 1 << 30)
            return self._resolve(name, self.root, position)
        if name in current.nonlocals:
            parent = self._next_scope(current)
            if current.kind == "function":
                position = (1 << 30, 1 << 30, 1 << 30)
            return self._resolve(name, parent, position)

        events = [
            event for event in current.bindings.get(name, ()) if event.position <= position
        ]
        definite = max(
            (event for event in events if not event.may_bind),
            key=lambda event: event.position,
            default=None,
        )
        cutoff = definite.position if definite is not None else (-1, -1, -1)
        uncertain = [
            event for event in events if event.may_bind and event.position > cutoff
        ]
        uncertain.extend(
            event
            for event in current.wildcard_imports
            if event.position <= position and event.position > cutoff
        )

        if definite is not None:
            base = BindingResolution(
                "import" if definite.import_target is not None else "shadowed",
                definite.import_target,
                scope=current,
                binding_position=definite.position[:2],
            )
        elif current.kind in {"function", "comprehension"} and name in current.local_names:
            base = BindingResolution("shadowed", scope=current)
        else:
            parent_position = position
            if current.kind == "function":
                # A deferred function body resolves free names when it is called,
                # after its defining module/class body has normally completed.
                parent_position = (1 << 30, 1 << 30, 1 << 30)
            base = self._resolve(name, self._next_scope(current), parent_position)

        if not uncertain:
            return base
        possible_imports = set(base.possible_import_targets)
        if base.state == "import" and base.import_target is not None:
            possible_imports.add(base.import_target)
        possible_imports.update(
            event.import_target
            for event in uncertain
            if event.import_target is not None
        )
        latest = max(uncertain, key=lambda event: event.position)
        return BindingResolution(
            "ambiguous",
            possible_import_targets=frozenset(possible_imports),
            scope=current,
            binding_position=latest.position[:2],
        )

    def is_import(self, name: str, target: str, scope: Scope, node: ast.AST) -> bool:
        binding = self.resolve(name, scope, node)
        return binding.state == "import" and binding.import_target == target

    def is_unshadowed_builtin(self, name: str, scope: Scope, node: ast.AST) -> bool:
        return self.resolve(name, scope, node).state == "unbound"


def _assignment_key(qualname: list[str], target: ast.AST) -> str | None:
    if not isinstance(target, ast.Name):
        return None
    return ".".join([*qualname, target.id])


@dataclass(eq=False)
class RegistryDefinition:
    position: tuple[int, int]
    value: ast.AST
    key: str
    name: str
    scope: Scope
    may_bind: bool = False


@dataclass
class RegistryIndex:
    definitions: dict[str, list[RegistryDefinition]] = field(default_factory=dict)
    by_binding: dict[tuple[Scope, str, tuple[int, int]], RegistryDefinition] = field(
        default_factory=dict
    )
    fragments: dict[RegistryDefinition, list[tuple[tuple[int, int], ast.AST]]] = field(
        default_factory=dict
    )


def _node_end(node: ast.AST) -> tuple[int, int]:
    return (
        getattr(node, "end_lineno", getattr(node, "lineno", 0)),
        getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    )


def _collect_registry_values(
    tree: ast.AST,
    scopes: dict[ast.AST, Scope] | None = None,
    root_scope: Scope | None = None,
) -> RegistryIndex:
    if scopes is None or root_scope is None:
        scope_builder = ScopeBuilder()
        scope_builder.visit(tree)
        scopes = scope_builder.scopes
        root_scope = scope_builder.root
    index = RegistryIndex()
    try_statement_types = (ast.Try,)
    try_star = getattr(ast, "TryStar", None)
    if try_star is not None:
        try_statement_types += (try_star,)

    def record_definitions(
        target: ast.AST,
        value: ast.AST,
        qualname: list[str],
        event: ast.AST,
        scope: Scope,
        may_bind: bool,
    ) -> None:
        position = _node_end(event)
        for name in _bound_names(target):
            key = ".".join([*qualname, name])
            definition = RegistryDefinition(position, value, key, name, scope, may_bind)
            index.definitions.setdefault(key, []).append(definition)
            index.by_binding[(scope, name, position)] = definition
            index.fragments[definition] = [(position, value)]

    def record_mutation(
        target: ast.Subscript,
        value: ast.AST,
        qualname: list[str],
        position: tuple[int, int],
    ) -> None:
        if not isinstance(target.value, ast.Name):
            return
        name = target.value.id
        for end in range(len(qualname), -1, -1):
            key = ".".join([*qualname[:end], name])
            available = [
                definition
                for definition in index.definitions.get(key, ())
                if definition.position <= position and not definition.may_bind
            ]
            if available:
                definition = max(available, key=lambda item: item.position)
                index.fragments[definition].append(
                    (position, ast.Dict(keys=[target.slice], values=[value]))
                )
                return

    def walk_statements(
        statements: Iterable[ast.stmt],
        qualname: list[str],
        scope: Scope,
        may_bind: bool = False,
    ) -> None:
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk_statements(
                    statement.body,
                    [*qualname, statement.name],
                    scopes[statement],
                )
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                value = statement.value
                if value is not None:
                    for target in targets:
                        record_definitions(
                            target, value, qualname, statement, scope, may_bind
                        )
                        if isinstance(target, ast.Subscript):
                            record_mutation(target, value, qualname, _node_end(statement))
            elif isinstance(statement, ast.If):
                branch_may_bind = may_bind or scope.kind in {"module", "class"}
                walk_statements(statement.body, qualname, scope, branch_may_bind)
                walk_statements(statement.orelse, qualname, scope, branch_may_bind)
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                record_definitions(
                    statement.target,
                    statement.iter,
                    qualname,
                    statement.target,
                    scope,
                    may_bind or scope.kind in {"module", "class"},
                )
                branch_may_bind = may_bind or scope.kind in {"module", "class"}
                walk_statements(statement.body, qualname, scope, branch_may_bind)
                walk_statements(statement.orelse, qualname, scope, branch_may_bind)
            elif isinstance(statement, (ast.While, ast.With, ast.AsyncWith)):
                branch_may_bind = may_bind or (
                    isinstance(statement, ast.While)
                    and scope.kind in {"module", "class"}
                )
                walk_statements(statement.body, qualname, scope, branch_may_bind)
                walk_statements(
                    getattr(statement, "orelse", ()), qualname, scope, branch_may_bind
                )
            elif isinstance(statement, try_statement_types):
                branch_may_bind = may_bind or scope.kind in {"module", "class"}
                walk_statements(statement.body, qualname, scope, branch_may_bind)
                walk_statements(statement.orelse, qualname, scope, branch_may_bind)
                walk_statements(statement.finalbody, qualname, scope, branch_may_bind)
                for handler in statement.handlers:
                    walk_statements(handler.body, qualname, scope, branch_may_bind)
            elif isinstance(statement, ast.Match):
                branch_may_bind = may_bind or scope.kind in {"module", "class"}
                for case in statement.cases:
                    walk_statements(case.body, qualname, scope, branch_may_bind)

    walk_statements(getattr(tree, "body", ()), [], root_scope)
    return index


def _strings_from_registry_value(node: ast.AST, value_part: str) -> set[str] | None:
    if not isinstance(node, ast.Dict):
        return None
    candidates: set[str] = set()
    for value in node.values:
        selected = value
        if value_part == "tuple_first":
            if not isinstance(value, (ast.Tuple, ast.List)) or not value.elts:
                return None
            selected = value.elts[0]
        if not isinstance(selected, ast.Constant) or not isinstance(selected.value, str):
            return None
        candidate = selected.value
        if value_part == "without_last_component":
            candidate, separator, _ = candidate.rpartition(".")
            if not separator:
                return None
        candidates.add(candidate)
    return candidates


def _registry_candidates(
    module: str,
    qualname: str,
    registries: RegistryIndex,
    argument: ast.AST,
    resolver: BindingResolver | None = None,
    scope: Scope | None = None,
) -> set[str] | None:
    rule = DYNAMIC_IMPORT_REGISTRIES.get((module, qualname))
    if rule is None:
        return None
    if rule.variable:
        if resolver is None or scope is None:
            return None
        evaluated = _enumerate_registry_expression(
            argument,
            module,
            rule,
            registries,
            resolver,
            scope,
            frozenset(),
        )
        if evaluated is None or not evaluated[1]:
            return None
        candidates = evaluated[0]
    elif rule.fixed_modules:
        if _static_module_expression(argument, module) not in rule.fixed_modules:
            return None
        candidates = set(rule.fixed_modules)
    else:
        return None
    resolved: set[str] = set()
    for candidate in candidates:
        if rule.plain_values_are_relative and not candidate.startswith("src."):
            if not rule.relative_package:
                return None
            candidate = f"{rule.relative_package}.{candidate}"
        elif candidate.startswith("."):
            if not rule.relative_package:
                return None
            level = len(candidate) - len(candidate.lstrip("."))
            candidate = _resolve_relative(
                rule.relative_package, level, candidate[level:] or None
            ) or ""
            if not candidate:
                return None
        resolved.add(candidate)
    return resolved


def _attribute_path(node: ast.AST) -> tuple[str, ...] | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _registry_path_matches(
    node: ast.AST, variable: str, qualname: list[str]
) -> bool:
    path = _attribute_path(node)
    if path is None:
        return False
    rendered = ".".join(path)
    if rendered == variable:
        return True
    for end in range(len(qualname), 0, -1):
        if ".".join([*qualname[:end], rendered]) == variable:
            return True
    if path[0] in {"self", "cls"} and qualname:
        return ".".join([qualname[0], *path[1:]]) == variable
    return False


def _definition_for_name(
    node: ast.Name,
    registries: RegistryIndex,
    resolver: BindingResolver,
    scope: Scope,
) -> RegistryDefinition | None:
    binding = resolver.resolve(node.id, scope, node)
    if (
        binding.state != "shadowed"
        or binding.scope is None
        or binding.binding_position is None
    ):
        return None
    definition = registries.by_binding.get(
        (binding.scope, node.id, binding.binding_position)
    )
    if definition is None or definition.may_bind:
        return None
    return definition


def _definition_for_exact_path(
    variable: str,
    registries: RegistryIndex,
    position: tuple[int, int],
) -> RegistryDefinition | None:
    definitions = [
        definition
        for definition in registries.definitions.get(variable, ())
        if definition.position <= position
    ]
    definite = max(
        (definition for definition in definitions if not definition.may_bind),
        key=lambda definition: definition.position,
        default=None,
    )
    cutoff = definite.position if definite is not None else (-1, -1)
    if any(
        definition.may_bind and definition.position > cutoff
        for definition in definitions
    ):
        return None
    return definite


def _registry_definition_values(
    definition: RegistryDefinition,
    rule: DynamicRegistryRule,
    registries: RegistryIndex,
    position: tuple[int, int],
) -> set[str] | None:
    candidates: set[str] = set()
    for fragment_position, node in registries.fragments.get(definition, ()):
        if fragment_position > position and definition.scope.kind in {"module", "class"}:
            continue
        extracted = _strings_from_registry_value(node, rule.value_part)
        if extracted is None:
            if rule.variable and rule.variable.endswith("possible_imports") and isinstance(
                node, ast.List
            ):
                extracted = set()
                for item in node.elts:
                    if (
                        not isinstance(item, ast.Tuple)
                        or not item.elts
                        or not isinstance(item.elts[0], ast.Constant)
                        or not isinstance(item.elts[0].value, str)
                    ):
                        return None
                    extracted.add(item.elts[0].value)
            else:
                return None
        candidates.update(extracted)
    return candidates


def _combine_string_sets(left: set[str], right: set[str]) -> set[str]:
    return {left_value + right_value for left_value in left for right_value in right}


def _enumerate_registry_expression(
    node: ast.AST,
    module: str,
    rule: DynamicRegistryRule,
    registries: RegistryIndex,
    resolver: BindingResolver,
    scope: Scope,
    seen: frozenset[RegistryDefinition],
) -> tuple[set[str], bool] | None:
    position = (
        getattr(node, "lineno", 0),
        getattr(node, "col_offset", 0),
    )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}, False
    if isinstance(node, ast.Name) and node.id == "__name__":
        return {module}, False
    if isinstance(node, ast.Name):
        definition = _definition_for_name(node, registries, resolver, scope)
        if definition is None or definition in seen:
            return None
        if definition.key == rule.variable:
            values = _registry_definition_values(
                definition, rule, registries, position
            )
            return None if values is None else (values, True)
        return _enumerate_registry_expression(
            definition.value,
            module,
            rule,
            registries,
            resolver,
            definition.scope,
            seen | {definition},
        )
    if isinstance(node, ast.Attribute):
        path = _attribute_path(node)
        rendered = ".".join(path or ())
        self_path = bool(
            path
            and path[0] in {"self", "cls"}
            and rule.variable
            and rule.variable.endswith("." + ".".join(path[1:]))
        )
        if rule.variable is None or not (rendered == rule.variable or self_path):
            return None
        definition = _definition_for_exact_path(rule.variable, registries, position)
        if definition is None:
            return None
        values = _registry_definition_values(definition, rule, registries, position)
        return None if values is None else (values, True)
    if isinstance(node, ast.Subscript):
        return _enumerate_registry_expression(
            node.value, module, rule, registries, resolver, scope, seen
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr not in {"get", "rsplit", "rpartition"}:
            return None
        if node.func.attr == "get" and (
            len(node.args) != 1
            or isinstance(node.args[0], ast.Starred)
            or node.keywords
        ):
            return None
        if node.func.attr in {"rsplit", "rpartition"} and any(
            not isinstance(argument, ast.Constant) for argument in node.args
        ):
            return None
        return _enumerate_registry_expression(
            node.func.value, module, rule, registries, resolver, scope, seen
        )
    if isinstance(node, ast.FormattedValue):
        if node.conversion != -1 or node.format_spec is not None:
            return None
        return _enumerate_registry_expression(
            node.value, module, rule, registries, resolver, scope, seen
        )
    if isinstance(node, ast.JoinedStr):
        values = {""}
        derived = False
        for part in node.values:
            evaluated = _enumerate_registry_expression(
                part, module, rule, registries, resolver, scope, seen
            )
            if evaluated is None:
                return None
            values = _combine_string_sets(values, evaluated[0])
            derived = derived or evaluated[1]
        return values, derived
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _enumerate_registry_expression(
            node.left, module, rule, registries, resolver, scope, seen
        )
        right = _enumerate_registry_expression(
            node.right, module, rule, registries, resolver, scope, seen
        )
        if left is None or right is None:
            return None
        return _combine_string_sets(left[0], right[0]), left[1] or right[1]
    return None


def _static_module_expression(node: ast.AST, module: str) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id == "__name__":
        return module
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            selected = value.value if isinstance(value, ast.FormattedValue) else value
            part = _static_module_expression(selected, module)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_module_expression(node.left, module)
        right = _static_module_expression(node.right, module)
        if left is not None and right is not None:
            return left + right
    return None


class ImportVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        path: str,
        module: str,
        is_package: bool,
        source_index: ModuleIndex,
        artifact_index: ModuleIndex,
        scopes: dict[ast.AST, Scope],
        root_scope: Scope,
        registries: RegistryIndex,
    ) -> None:
        self.path = path
        self.module = module
        self.package = module if is_package else module.rpartition(".")[0]
        self.source_index = source_index
        self.artifact_index = artifact_index
        self.scopes = scopes
        self.scope = root_scope
        self.resolver = BindingResolver(root_scope)
        self.registries = registries
        self.errors: set[CheckError] = set()
        self._inside_import_error_try = 0
        self._qualname: list[str] = []

    def _record_missing(self, module: str, line: int) -> None:
        if self.artifact_index.contains_required(self.source_index, module):
            return
        if self._inside_import_error_try and module in IMPORT_ERROR_ALLOWLIST:
            return
        self.errors.add(CheckError(self.path, line, module))

    def _check_absolute(self, name: str, line: int) -> str | None:
        module = self.source_index.canonicalize_absolute(name)
        if module is not None:
            self._record_missing(module, line)
        return module

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_absolute(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            base = _resolve_relative(self.package, node.level, node.module)
            if base is None:
                self.errors.add(CheckError(self.path, node.lineno, "invalid relative import"))
                return
        elif node.module:
            base = self.source_index.canonicalize_absolute(node.module)
            if base is None:
                return
        else:
            return

        self._record_missing(base, node.lineno)
        for alias in node.names:
            if alias.name == "*":
                continue
            candidate = f"{base}.{alias.name}"
            if candidate in self.source_index.importable_names:
                self._record_missing(candidate, node.lineno)

    @staticmethod
    def _argument(node: ast.Call, position: int, keyword: str) -> ast.AST | None:
        if len(node.args) > position:
            return node.args[position]
        for item in node.keywords:
            if item.arg == keyword:
                return item.value
        return None

    @staticmethod
    def _literal_string(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    @staticmethod
    def _literal_integer(node: ast.AST | None, default: int) -> int | None:
        if node is None:
            return default
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        return None

    def _import_module_call_state(self, node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            binding = self.resolver.resolve(node.func.id, self.scope, node.func)
            if (
                binding.state == "import"
                and binding.import_target == "importlib.import_module"
            ):
                return "matched"
            if binding.state == "ambiguous" and (
                node.func.id == "import_module"
                or "importlib.import_module" in binding.possible_import_targets
            ):
                return "ambiguous"
            return "none"
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and isinstance(node.func.value, ast.Name)
        ):
            binding = self.resolver.resolve(node.func.value.id, self.scope, node.func.value)
            if binding.state == "import" and binding.import_target == "importlib":
                return "matched"
            if binding.state == "ambiguous" and (
                node.func.value.id == "importlib"
                or "importlib" in binding.possible_import_targets
            ):
                return "ambiguous"
        return "none"

    def _builtin_import_call_state(self, node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            binding = self.resolver.resolve(node.func.id, self.scope, node.func)
            if (
                binding.state == "import"
                and binding.import_target == "builtins.__import__"
            ):
                return "matched"
            if node.func.id == "__import__":
                if binding.state == "unbound":
                    return "matched"
                if binding.state == "ambiguous":
                    return "ambiguous"
            if (
                binding.state == "ambiguous"
                and "builtins.__import__" in binding.possible_import_targets
            ):
                return "ambiguous"
            return "none"
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "__import__"
            and isinstance(node.func.value, ast.Name)
        ):
            binding = self.resolver.resolve(node.func.value.id, self.scope, node.func.value)
            if binding.state == "import" and binding.import_target == "builtins":
                return "matched"
            if binding.state == "ambiguous" and (
                node.func.value.id == "builtins"
                or "builtins" in binding.possible_import_targets
            ):
                return "ambiguous"
        return "none"

    def _check_registry_or_fail(self, line: int, argument: ast.AST) -> bool:
        qualname = ".".join(self._qualname)
        candidates = _registry_candidates(
            self.module,
            qualname,
            self.registries,
            argument,
            self.resolver,
            self.scope,
        )
        if candidates is None:
            self.errors.add(CheckError(self.path, line, "non-literal dynamic import"))
            return False
        for candidate in sorted(candidates):
            self._check_absolute(candidate, line)
        return True

    def _visit_import_module_call(self, node: ast.Call) -> None:
        name_node = self._argument(node, 0, "name")
        name = self._literal_string(name_node)
        if name is None:
            self._check_registry_or_fail(node.lineno, name_node or node)
            return
        if name.startswith("."):
            package_node = self._argument(node, 1, "package")
            package = self._literal_string(package_node)
            if package is None:
                self.errors.add(CheckError(self.path, node.lineno, "non-literal dynamic import"))
                return
            level = len(name) - len(name.lstrip("."))
            resolved = _resolve_relative(package, level, name[level:] or None)
            if resolved is None:
                self.errors.add(CheckError(self.path, node.lineno, "invalid relative import"))
            else:
                self._check_absolute(resolved, node.lineno)
        else:
            self._check_absolute(name, node.lineno)

    def _relative_builtin_package(self, node: ast.Call) -> str | None:
        globals_node = self._argument(node, 1, "globals")
        if (
            isinstance(globals_node, ast.Call)
            and isinstance(globals_node.func, ast.Name)
            and globals_node.func.id == "globals"
            and not globals_node.args
            and not globals_node.keywords
            and self.resolver.is_unshadowed_builtin(
                "globals", self.scope, globals_node.func
            )
        ):
            return self.package
        if isinstance(globals_node, ast.Dict):
            if any(key is None for key in globals_node.keys):
                return None
            packages: list[str] = []
            for key, value in zip(globals_node.keys, globals_node.values):
                if self._literal_string(key) == "__package__":
                    package = self._literal_string(value)
                    if package is None:
                        return None
                    packages.append(package)
            if len(packages) == 1:
                return packages[0]
        return None

    def _visit_builtin_import_call(self, node: ast.Call) -> None:
        name_node = self._argument(node, 0, "name")
        name = self._literal_string(name_node)
        level = self._literal_integer(self._argument(node, 4, "level"), 0)
        if name is None or level is None:
            if not self._check_registry_or_fail(node.lineno, name_node or node):
                return
            fromlist_node = self._argument(node, 3, "fromlist")
            if fromlist_node is not None and not isinstance(
                fromlist_node, (ast.Tuple, ast.List)
            ):
                self._check_registry_or_fail(node.lineno, fromlist_node)
            return
        if level:
            package = self._relative_builtin_package(node)
            if package is None:
                self.errors.add(CheckError(self.path, node.lineno, "non-literal dynamic import"))
                return
            resolved = _resolve_relative(package, level, name or None)
            if resolved is None:
                self.errors.add(CheckError(self.path, node.lineno, "invalid relative import"))
                return
        elif name.startswith("."):
            self.errors.add(CheckError(self.path, node.lineno, "invalid relative import"))
            return
        else:
            resolved = name
        base = self._check_absolute(resolved, node.lineno)
        fromlist_node = self._argument(node, 3, "fromlist")
        if base is None or fromlist_node is None:
            return
        if not isinstance(fromlist_node, (ast.Tuple, ast.List)):
            self.errors.add(CheckError(self.path, node.lineno, "non-literal dynamic import"))
            return
        for item in fromlist_node.elts:
            item_name = self._literal_string(item)
            if item_name is None:
                self.errors.add(CheckError(self.path, node.lineno, "non-literal dynamic import"))
                return
            candidate = f"{base}.{item_name}"
            if candidate in self.source_index.importable_names:
                self._record_missing(candidate, node.lineno)

    def visit_Call(self, node: ast.Call) -> None:
        import_module_state = self._import_module_call_state(node)
        builtin_import_state = self._builtin_import_call_state(node)
        if "ambiguous" in {import_module_state, builtin_import_state}:
            self.errors.add(CheckError(self.path, node.lineno, "non-literal dynamic import"))
        elif import_module_state == "matched":
            self._visit_import_module_call(node)
        elif builtin_import_state == "matched":
            self._visit_builtin_import_call(node)
        self.generic_visit(node)

    def _is_type_checking_test(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return self.resolver.is_import(
                node.id, "typing.TYPE_CHECKING", self.scope, node
            )
        return bool(
            isinstance(node, ast.Attribute)
            and node.attr == "TYPE_CHECKING"
            and isinstance(node.value, ast.Name)
            and self.resolver.is_import(node.value.id, "typing", self.scope, node.value)
        )

    def visit_If(self, node: ast.If) -> None:
        if self._is_type_checking_test(node.test):
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default:
                self.visit(default)
        if node.returns:
            self.visit(node.returns)
        previous_scope = self.scope
        previous_depth = self._inside_import_error_try
        self.scope = self.scopes[node]
        self._inside_import_error_try = 0
        self._qualname.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self._qualname.pop()
        self._inside_import_error_try = previous_depth
        self.scope = previous_scope

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        previous_scope = self.scope
        previous_depth = self._inside_import_error_try
        self.scope = self.scopes[node]
        self._inside_import_error_try = 0
        self.visit(node.body)
        self._inside_import_error_try = previous_depth
        self.scope = previous_scope

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        previous_scope = self.scope
        self.scope = self.scopes[node]
        self._qualname.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self._qualname.pop()
        self.scope = previous_scope

    def _visit_comprehension(self, node: ast.AST) -> None:
        previous_scope = self.scope
        generators = node.generators  # type: ignore[attr-defined]
        if not generators:
            return
        self.visit(generators[0].iter)
        self.scope = self.scopes[node]
        for index, generator in enumerate(generators):
            if index:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)  # type: ignore[attr-defined]
        self.scope = previous_scope

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension

    def _catches_import_error(self, handler: ast.ExceptHandler) -> bool:
        def is_import_error(node: ast.expr | None) -> bool:
            if isinstance(node, ast.Name):
                return node.id in {"ImportError", "ModuleNotFoundError"} and (
                    self.resolver.is_unshadowed_builtin(node.id, self.scope, node)
                )
            if isinstance(node, ast.Attribute):
                return bool(
                    node.attr in {"ImportError", "ModuleNotFoundError"}
                    and isinstance(node.value, ast.Name)
                    and self.resolver.is_import(
                        node.value.id, "builtins", self.scope, node.value
                    )
                )
            if isinstance(node, ast.Tuple):
                return any(is_import_error(item) for item in node.elts)
            return False

        return is_import_error(handler.type)

    def visit_Try(self, node: ast.Try) -> None:
        catches_import_error = any(self._catches_import_error(h) for h in node.handlers)
        if catches_import_error:
            self._inside_import_error_try += 1
        for statement in node.body:
            self.visit(statement)
        if catches_import_error:
            self._inside_import_error_try -= 1
        for handler in node.handlers:
            self.visit(handler)
        for statement in node.orelse:
            self.visit(statement)
        for statement in node.finalbody:
            self.visit(statement)


def _roots_overlap(artifact_root: Path, source_root: Path) -> CheckError | None:
    try:
        if os.path.samefile(artifact_root, source_root):
            return CheckError(".", 1, "artifact and source roots must be different")
        artifact_resolved = artifact_root.resolve(strict=True)
        source_resolved = source_root.resolve(strict=True)
    except OSError as error:
        return CheckError(".", 1, f"root relationship check failed: {error}")
    if artifact_resolved in source_resolved.parents or source_resolved in artifact_resolved.parents:
        return CheckError(".", 1, "artifact and source roots must not be parent/child")
    return None


def check_import_closure(artifact_root: Path, source_root: Path) -> list[CheckError]:
    artifact_scan = _scan_python_files(artifact_root, "artifact")
    source_scan = _scan_python_files(source_root, "source")
    errors = artifact_scan.errors | source_scan.errors
    if not artifact_scan.valid or not source_scan.valid:
        return sorted(errors)

    overlap = _roots_overlap(artifact_root, source_root)
    if overlap:
        errors.add(overlap)
        return sorted(errors)

    source_index = build_module_index(source_root, source_scan.files)
    artifact_index = build_module_index(artifact_root, artifact_scan.files)

    # If an artifact contains any part of a source package, it must retain the
    # source package's initializer rather than silently turning it into a
    # namespace package.
    represented_prefixes = _represented_package_prefixes(
        artifact_index.importable_names
    )
    for package in source_index.package_initializers:
        if (
            package in represented_prefixes
            and package not in artifact_index.package_initializers
        ):
            errors.add(
                CheckError(
                    f"{package.replace('.', '/')}/__init__.py",
                    1,
                    f"missing package initializer: {package}",
                )
            )

    for path in artifact_scan.files:
        relative_path = path.relative_to(artifact_root).as_posix()
        try:
            with tokenize.open(path) as source_file:
                source = source_file.read()
        except (UnicodeError, OSError) as error:
            errors.add(CheckError(relative_path, 1, f"compile error: {error}"))
            continue
        try:
            compile(source, relative_path, "exec")
        except RecursionError:
            errors.add(
                CheckError(relative_path, 1, "compile error: recursion limit exceeded")
            )
            continue
        except SyntaxError as error:
            line = getattr(error, "lineno", None) or 1
            message = getattr(error, "msg", None) or str(error)
            errors.add(CheckError(relative_path, line, f"compile error: {message}"))
            continue
        try:
            tree = ast.parse(source, filename=relative_path)
        except RecursionError:
            errors.add(
                CheckError(relative_path, 1, "parse error: recursion limit exceeded")
            )
            continue
        except SyntaxError as error:
            line = getattr(error, "lineno", None) or 1
            message = getattr(error, "msg", None) or str(error)
            errors.add(CheckError(relative_path, line, f"parse error: {message}"))
            continue

        scope_builder = ScopeBuilder()
        try:
            scope_builder.visit(tree)
            registries = _collect_registry_values(
                tree, scope_builder.scopes, scope_builder.root
            )
        except RecursionError:
            errors.add(
                CheckError(relative_path, 1, "analysis error: recursion limit exceeded")
            )
            continue
        module = _module_name(artifact_root, path)
        visitor = ImportVisitor(
            path=relative_path,
            module=module,
            is_package=path.name == "__init__.py",
            source_index=source_index,
            artifact_index=artifact_index,
            scopes=scope_builder.scopes,
            root_scope=scope_builder.root,
            registries=registries,
        )
        try:
            visitor.visit(tree)
        except RecursionError:
            errors.add(
                CheckError(relative_path, 1, "analysis error: recursion limit exceeded")
            )
            continue
        errors.update(visitor.errors)

    return sorted(errors)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    errors = check_import_closure(args.artifact_root.absolute(), args.source_root.absolute())
    for error in errors:
        print(error.render())
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
