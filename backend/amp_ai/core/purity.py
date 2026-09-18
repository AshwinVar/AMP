"""Structural guards for AMP-native AI code: standard library only, no forbidden imports, no dynamic code.

WHY AN AST SCAN
---------------
A text search is both too loose and too weak: ``eval(`` matches ``build_eval.py``,
while ``importlib.import_module("numpy")`` contains no ``import numpy`` at all.
These guards parse each file and inspect the actual import statements and names.

WHAT IS FOLLOWED
----------------
Importing ``pkg.mod`` executes ``pkg/__init__.py`` first. A "pure" module inside a
package whose ``__init__`` eagerly imports a database layer is not pure in
practice, so imports that resolve to LOCAL files (relative imports, and absolute
imports of modules found next to the package root) are followed transitively and
the files they execute are checked by the same rules. The root is the first
ancestor directory without an ``__init__.py`` (``backend/`` here).

Standard-library and explicitly allowed third-party names are not followed.

THE RULES
---------
``assert_stdlib_only(paths, min_files=, extra_allowed=())``
    Every import must be standard library (``sys.stdlib_module_names``), a
    RELATIVE import (followed), or a top-level name in ``extra_allowed``
    (followed if it is local, e.g. ``{"duration"}`` or ``{"amp_ai"}``). Modules in
    ``FORBIDDEN_MODULES`` are refused even if listed in ``extra_allowed``: ML
    stacks, network clients, serialisation that executes code, native-code
    loaders and process spawning. Dynamic code is refused too (below).
``assert_no_forbidden_imports(paths, forbidden, min_files=, transitive=True)``
    No import of any dotted name in ``forbidden`` or below it (``"ai"`` refuses
    ``ai`` and ``ai.maintenance`` but not ``amp_ai``); ``from ai import
    prediction`` counts as ``ai.prediction``.
``assert_no_dynamic_code(paths, min_files=)``
    No pickle/marshal/shelve/dill/cloudpickle/joblib import, and no reference to
    ``eval``, ``exec``, ``compile``, ``__import__`` or ``import_module``
    (``builtins.eval`` included). ``re.compile`` is an attribute of ``re`` and is
    allowed. Checks only the files given.

A GUARD THAT MATCHES NOTHING REPORTS ALL-CLEAR
----------------------------------------------
So every guard requires ``min_files >= 1``, refuses a path that does not exist,
and fails when fewer than ``min_files`` files were scanned. Violations raise
``PurityViolation`` (an ``AssertionError``) listing every problem found.
"""
import ast
import os
import sys
from collections import deque
from pathlib import Path

__all__ = [
    "PurityViolation", "FORBIDDEN_MODULES", "SERIALIZATION_MODULES", "DYNAMIC_CODE_NAMES",
    "assert_stdlib_only", "assert_no_forbidden_imports", "assert_no_dynamic_code",
]

SERIALIZATION_MODULES = frozenset({
    "pickle", "_pickle", "cPickle", "marshal", "shelve", "dill", "cloudpickle", "joblib",
})
FORBIDDEN_MODULES = SERIALIZATION_MODULES | frozenset({
    # numerical / ML stacks and model runtimes
    "numpy", "scipy", "pandas", "sklearn", "torch", "tensorflow", "keras", "jax",
    "onnx", "onnxruntime", "xgboost", "lightgbm", "transformers",
    # network
    "urllib", "urllib3", "http", "socket", "ssl", "requests", "httpx", "aiohttp",
    "ftplib", "smtplib", "imaplib", "poplib", "telnetlib", "xmlrpc", "websockets",
    # native code and processes
    "ctypes", "cffi", "subprocess",
})
DYNAMIC_CODE_NAMES = frozenset({"eval", "exec", "compile", "__import__", "import_module"})


class PurityViolation(AssertionError):
    def __init__(self, violations):
        self.violations = list(violations)
        super().__init__("purity check failed:\n  " + "\n  ".join(self.violations))


class _Import:
    __slots__ = ("lineno", "module", "names", "level")

    def __init__(self, lineno, module, names, level):
        self.lineno = lineno
        self.module = module      # absolute dotted name; None if a relative import cannot be resolved
        self.names = names        # names after "from X import"
        self.level = level        # 0 for absolute imports


class _Facts:
    __slots__ = ("path", "root", "imports", "dynamic", "error")

    def __init__(self, path, root):
        self.path = path
        self.root = root
        self.imports = []
        self.dynamic = []
        self.error = None


# --------------------------------------------------------------------------- arguments
def _check_min_files(min_files):
    if isinstance(min_files, bool) or not isinstance(min_files, int) or min_files < 1:
        raise ValueError("min_files must be an int >= 1: a guard that expects to find nothing proves nothing")


def _name_set(values, label):
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{label} must be a collection of names, not a single string")
    names = set(values)
    if not all(isinstance(n, str) and n for n in names):
        raise TypeError(f"{label} must contain non-empty strings")
    return names


def _expand(paths):
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    files, missing, seen = [], [], set()
    for p in paths:
        path = Path(os.fsdecode(p) if isinstance(p, bytes) else os.fspath(p))
        if path.is_dir():
            candidates = sorted(path.rglob("*.py"))
        elif path.is_file():
            candidates = [path]
        else:
            missing.append(str(path))
            continue
        for c in candidates:
            key = c.resolve()
            if key not in seen:
                seen.add(key)
                files.append(key)
    return files, missing


def _preamble(paths, min_files):
    _check_min_files(min_files)
    files, missing = _expand(paths)
    violations = [f"{m}: path does not exist" for m in missing]
    if len(files) < min_files:
        violations.append(f"scanned {len(files)} file(s), expected at least {min_files}")
    return files, violations


# --------------------------------------------------------------------------- parsing
def _locate(path):
    directory = path.parent
    package = []
    while (directory / "__init__.py").is_file():
        package.insert(0, directory.name)
        directory = directory.parent
    return directory, package


def _resolve(package, level, module):
    if level == 0:
        return module
    if level - 1 > len(package):
        return None
    parts = package[:len(package) - (level - 1)] + (module.split(".") if module else [])
    return ".".join(parts) if parts else None


def _facts(path):
    root, package = _locate(path)
    facts = _Facts(path, root)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
        facts.error = f"{path}: cannot be parsed ({exc.__class__.__name__})"
        return facts
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                facts.imports.append(_Import(node.lineno, alias.name, (), 0))
        elif isinstance(node, ast.ImportFrom):
            names = tuple(a.name for a in node.names if a.name != "*")
            facts.imports.append(_Import(node.lineno, _resolve(package, node.level, node.module), names, node.level))
            for name in names:
                if name in DYNAMIC_CODE_NAMES:
                    facts.dynamic.append((node.lineno, f"imports {name}"))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in DYNAMIC_CODE_NAMES:
            facts.dynamic.append((node.lineno, f"uses {node.id}"))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if node.attr == "import_module":
                facts.dynamic.append((node.lineno, "uses import_module"))
            elif (node.attr in DYNAMIC_CODE_NAMES and isinstance(node.value, ast.Name)
                  and node.value.id in ("builtins", "__builtins__")):
                facts.dynamic.append((node.lineno, f"uses {node.value.id}.{node.attr}"))
    return facts


def _chain(root, dotted):
    """Files executed when importing ``dotted`` from ``root``, and whether the module itself was found."""
    files = []
    directory = root
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        package_init = directory / part / "__init__.py"
        if package_init.is_file():
            files.append(package_init)
            directory = directory / part
        elif i == len(parts) - 1 and (directory / f"{part}.py").is_file():
            files.append(directory / f"{part}.py")
        else:
            return files, False
    return files, True


def _targets(root, imp):
    """(files the import executes, base module found?). ``from X import n`` also runs submodule X.n if it exists."""
    files, found = _chain(root, imp.module)
    if not found:
        return files, False
    for name in imp.names:
        sub_files, sub_found = _chain(root, f"{imp.module}.{name}")
        if sub_found:
            files.extend(sub_files)
    return files, True


def _is_local(root, top):
    return (root / top / "__init__.py").is_file() or (root / f"{top}.py").is_file()


def _hits(imp, forbidden):
    candidates = [imp.module] + [f"{imp.module}.{n}" for n in imp.names]
    for candidate in candidates:
        for f in sorted(forbidden):  # sorted: the same violation message every run
            if candidate == f or candidate.startswith(f + "."):
                return f
    return None


def _where(facts, lineno):
    return f"{facts.path}:{lineno}"


# --------------------------------------------------------------------------- guards
def assert_stdlib_only(paths, *, min_files: int, extra_allowed=()) -> None:
    files, violations = _preamble(paths, min_files)
    extra = _name_set(extra_allowed, "extra_allowed")
    queue, seen = deque(files), set()
    while queue:
        path = queue.popleft().resolve()
        if path in seen:
            continue
        seen.add(path)
        facts = _facts(path)
        if facts.error:
            violations.append(facts.error)
            continue
        for lineno, description in facts.dynamic:
            violations.append(f"{_where(facts, lineno)}: dynamic code: {description}")
        for imp in facts.imports:
            where = _where(facts, imp.lineno)
            if imp.module is None:
                violations.append(f"{where}: relative import cannot be resolved")
                continue
            hit = _hits(imp, FORBIDDEN_MODULES)
            if hit:
                violations.append(f"{where}: forbidden module {hit!r}")
                continue
            top = imp.module.split(".")[0]
            if imp.level > 0:
                targets, found = _targets(facts.root, imp)
                if not found:
                    violations.append(f"{where}: relative import {imp.module!r} does not resolve to a file")
                    continue
                queue.extend(targets)
            elif _is_local(facts.root, top):
                if top not in extra:
                    violations.append(f"{where}: {imp.module!r} is a local module, not standard library "
                                      "(list it in extra_allowed if it is intended)")
                    continue
                targets, _ = _targets(facts.root, imp)
                queue.extend(targets)
            elif top in sys.stdlib_module_names or top in extra:
                continue
            else:
                violations.append(f"{where}: {imp.module!r} is not in the standard library")
    if violations:
        raise PurityViolation(violations)


def assert_no_forbidden_imports(paths, forbidden: set[str], *, min_files: int, transitive=True) -> None:
    banned = _name_set(forbidden, "forbidden")
    if not banned:
        raise ValueError("forbidden must name at least one module")
    files, violations = _preamble(paths, min_files)
    queue, seen = deque(files), set()
    while queue:
        path = queue.popleft().resolve()
        if path in seen:
            continue
        seen.add(path)
        facts = _facts(path)
        if facts.error:
            violations.append(facts.error)
            continue
        for imp in facts.imports:
            where = _where(facts, imp.lineno)
            if imp.module is None:
                violations.append(f"{where}: relative import cannot be resolved")
                continue
            hit = _hits(imp, banned)
            if hit:
                violations.append(f"{where}: imports forbidden {hit!r} ({imp.module})")
                continue
            if transitive and (imp.level > 0 or _is_local(facts.root, imp.module.split(".")[0])):
                targets, _ = _targets(facts.root, imp)
                queue.extend(targets)
    if violations:
        raise PurityViolation(violations)


def assert_no_dynamic_code(paths, *, min_files: int) -> None:
    files, violations = _preamble(paths, min_files)
    for path in files:
        facts = _facts(path)
        if facts.error:
            violations.append(facts.error)
            continue
        for lineno, description in facts.dynamic:
            violations.append(f"{_where(facts, lineno)}: dynamic code: {description}")
        for imp in facts.imports:
            if imp.module is None:
                continue
            hit = _hits(imp, SERIALIZATION_MODULES)
            if hit:
                violations.append(f"{_where(facts, imp.lineno)}: serialisation module {hit!r}")
    if violations:
        raise PurityViolation(violations)
