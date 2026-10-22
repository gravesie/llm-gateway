"""A tripwire: no test should read a file listed in `.claude/bookkeeping.json`.

The pre-push hook skips the test step for a push that changes only the listed files
(scripts/check_push_scope.py). That is safe only if no test depends on them. This cannot
PROVE that; it fails on the evidence it can see in `tests/`:

1. a string constant naming a listed path (an exact entry's file name, or a `dir/**`
   entry's directory), or
2. a `glob`, `rglob`, `os.walk`, `listdir`, `scandir`, `iterdir` or `git ls-files` call whose
   root or pattern could reach a listed path. A call whose root or pattern cannot be read is
   reported too, so a person looks.

Not reads of the repo, so ignored: `ast.walk`, and anything rooted in pytest's `tmp_path`.
That includes a local assigned only tmp-rooted values in its function (or in the function
enclosing it), a parameter that is a fixture defined in the same file whose every return or
yield is tmp-rooted (given to tests and fixtures only, not to plain helpers), and a string
joined onto such a root (`tmp_path / "queue.json"`).

A flag that cannot be classified is cleared by hand with a trailing comment on the flagged
line (the first line of the call): `# bookkeeping-unread: ok <reason>`. The reason is
required, and a marker without one is itself reported. Every honoured marker is listed in
the output, passing or failing, so a person can audit them. A marker is a human's claim,
not a proof.

It also fails on an invalid list. With no list it skips, so it is safe in any repo.

WHAT IT MISSES. A test that reads a listed file indirectly, through code under test that
opens it by default (a checker called with no arguments that opens its own default path),
or that builds the path at run time. A tmp-derived name is tracked inside one function only
(plus the names it inherits from the function around it). The backstop is CI, which runs
the full suite on every PR and on `main`: a wrong skip costs a red PR, never a red `main`.

Choosing a list: run this check, and prefer fewer paths. Record:
docs/doctrine/verifying-your-own-work.md in dev-process.

Distributed verbatim with scripts/check_push_scope.py, whose list grammar it reuses.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import posixpath
import re
import sys
import tokenize
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIST_PATH = ROOT / ".claude" / "bookkeeping.json"
TESTS_DIR = Path(__file__).resolve().parent

# Names pytest hands out for directories it creates outside the repo.
TMP_NAMES = {"tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory"}
# Path methods whose result stays inside the directory they are called on.
TMP_KEEPING_METHODS = {"resolve", "absolute", "joinpath", "mktemp", "with_name", "with_suffix"}
WALKERS = {"glob", "iglob", "rglob", "walk", "listdir", "scandir", "iterdir"}
PATH_CLASSES = {"Path", "PurePath", "PosixPath", "PurePosixPath", "WindowsPath"}
MARKER = re.compile(r"#\s*bookkeeping-unread:\s*ok\b(.*)$")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
# Stands in for the value of a parameter pytest fills with a tmp path.
_TMP_VALUE = ast.Name(id="tmp_path")
_UNKNOWN = object()
_TMP = object()


def _load_scope_module():
    spec = importlib.util.spec_from_file_location(
        "check_push_scope", ROOT / "scripts" / "check_push_scope.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass looks its own module up here
    spec.loader.exec_module(module)
    return module


# Loaded once: the list's ConfigError must be the same class where it is raised and caught.
SCOPE = _load_scope_module()


# ---- what a string or a root can be, statically ---------------------------------------


def _leftmost_name(node: ast.AST) -> str | None:
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.BinOp):
            node = node.left
        else:
            return None


def _text_of(node: ast.AST) -> tuple[str, bool] | None:
    """(text, exact) for a string expression; text ends in `*` when only a prefix is known."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    if isinstance(node, ast.JoinedStr):
        prefix = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                prefix += part.value
            else:
                return (prefix + "*", False) if prefix else None
        return prefix, True
    if isinstance(node, ast.Call) and _is_join(node.func):
        parts = [_text_of(arg) for arg in node.args]
        if parts and all(part is not None and part[1] for part in parts):
            return posixpath.join(*(part[0] for part in parts)), True
    return None


def _is_join(func: ast.AST) -> bool:
    return isinstance(func, ast.Attribute) and func.attr == "join"


def _root_of(node: ast.AST, tmp: frozenset[str] = frozenset()) -> object:
    """The constant directory a path expression starts from, `_TMP`, or `_UNKNOWN`."""
    if _leftmost_name(node) in TMP_NAMES or _is_tmp_expr(node, tmp):
        return _TMP
    text = _text_of(node)
    if text is not None and text[1]:
        return text[0]
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name in PATH_CLASSES:
            parts = [_text_of(arg) for arg in node.args]
            if all(part is not None and part[1] for part in parts):
                return posixpath.join(*(part[0] for part in parts)) if parts else "."
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _root_of(node.left, tmp)
        if isinstance(left, str):
            right = _text_of(node.right)
            return posixpath.join(left, right[0]) if right and right[1] else left
        return left
    return _UNKNOWN


# ---- paths that live in pytest's tmp dirs ---------------------------------------------


def _is_tmp_expr(node: ast.AST, tmp: frozenset[str]) -> bool:
    """Whether a path expression is rooted in a pytest tmp dir or a tmp-derived local."""
    if isinstance(node, ast.Name):
        return node.id in TMP_NAMES or node.id in tmp
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.Div) and _is_tmp_expr(node.left, tmp)
    if isinstance(node, ast.Attribute):
        return node.attr == "parent" and _is_tmp_expr(node.value, tmp)
    if isinstance(node, ast.Subscript):
        parents = node.value
        return (
            isinstance(parents, ast.Attribute)
            and parents.attr == "parents"
            and _is_tmp_expr(parents.value, tmp)
        )
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in PATH_CLASSES:
            return bool(node.args) and _is_tmp_expr(node.args[0], tmp)
        return (
            isinstance(func, ast.Attribute)
            and func.attr in TMP_KEEPING_METHODS
            and _is_tmp_expr(func.value, tmp)
        )
    return False


def _own_nodes(func: ast.AST):
    """Nodes in a function's own scope: nested functions, lambdas and classes are not entered."""
    pending = list(ast.iter_child_nodes(func))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, (*FUNCTIONS, ast.Lambda, ast.ClassDef)):
            pending.extend(ast.iter_child_nodes(node))


def _is_fixture(func: ast.AST) -> bool:
    """Whether a function carries `@pytest.fixture` or `@fixture`, with or without a call."""
    for decorator in func.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name) and target.id == "fixture":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return isinstance(target.value, ast.Name) and target.value.id == "pytest"
    return False


def _takes_fixtures(func: ast.AST) -> bool:
    """Only a test or a fixture is handed fixtures by pytest; a plain helper is not."""
    return func.name.startswith("test") or _is_fixture(func)


def _bindings(
    func: ast.AST, tmp_fixtures: frozenset[str] = frozenset()
) -> dict[str, list[ast.AST | None]]:
    """name -> the value bound at each binding in `func`'s own scope.

    None means a binding whose value is unknown (a loop target, an import, a parameter
    that is not a pytest tmp fixture, a tuple unpacking, and so on).
    """
    found: dict[str, list[ast.AST | None]] = {}
    args = func.args
    given = TMP_NAMES | (tmp_fixtures if _takes_fixtures(func) else frozenset())
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
        if arg is not None:
            value = _TMP_VALUE if arg.arg in given else None
            found.setdefault(arg.arg, []).append(value)
    plain: set[int] = set()  # the Name targets of `x = value` and `x: T = value`
    for node in _own_nodes(func):
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
        if isinstance(target, ast.Name):
            plain.add(id(target))
            found.setdefault(target.id, []).append(node.value)
    for node in _own_nodes(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and id(node) not in plain:
            found.setdefault(node.id, []).append(None)
        elif isinstance(node, (*FUNCTIONS, ast.ClassDef)):
            found.setdefault(node.name, []).append(None)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            found.setdefault(node.name, []).append(None)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                found.setdefault((alias.asname or alias.name).split(".")[0], []).append(None)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            if not isinstance(node.op, ast.Div):  # `root /= "x"` keeps a tmp path tmp
                found.setdefault(node.target.id, []).append(None)
    return found


def _tmp_names(
    func: ast.AST, inherited: frozenset[str], tmp_fixtures: frozenset[str] = frozenset()
) -> frozenset[str]:
    """The locals of `func` that hold a tmp path: every binding is a tmp-rooted expression.

    A name the function rebinds is judged on its own bindings alone; a name it never binds
    is inherited from the function around it. A name ever bound to anything else is not tmp.
    """
    bindings = _bindings(func, tmp_fixtures)
    tmp = {name for name in inherited if name not in bindings}
    changed = True
    while changed:
        changed = False
        for name, values in bindings.items():
            if name not in tmp and all(
                value is not None and _is_tmp_expr(value, frozenset(tmp)) for value in values
            ):
                tmp.add(name)
                changed = True
    return frozenset(tmp)


def _fixture_results(func: ast.AST) -> list[ast.AST | None]:
    """What a fixture hands out: the value of every `return` and `yield` (None if bare)."""
    return [node.value for node in _own_nodes(func) if isinstance(node, (ast.Return, ast.Yield))]


def _tmp_fixtures(tree: ast.AST) -> frozenset[str]:
    """Names of this file's own fixtures that always hand out a tmp-rooted path.

    Every return and yield must be tmp-rooted. A fixture may build on another one found
    here. Fixtures from conftest.py are not looked at: they cannot be read from this file.
    """
    fixtures = [node for node in tree.body if isinstance(node, FUNCTIONS) and _is_fixture(node)]
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for func in fixtures:
            if func.name in found:
                continue
            tmp = _tmp_names(func, frozenset(), frozenset(found))
            results = _fixture_results(func)
            if results and all(r is not None and _is_tmp_expr(r, tmp) for r in results):
                found.add(func.name)
                changed = True
    return frozenset(found)


def _walk_with_tmp(
    node: ast.AST, tmp: frozenset[str] = frozenset(), tmp_fixtures: frozenset[str] = frozenset()
):
    """Yield (node, the tmp names in scope there). Module-level names are never tmp."""
    yield node, tmp
    inner = _tmp_names(node, tmp, tmp_fixtures) if isinstance(node, FUNCTIONS) else tmp
    for child in ast.iter_child_nodes(node):
        yield from _walk_with_tmp(child, inner, tmp_fixtures)


def _tmp_joined_parts(node: ast.AST, tmp: frozenset[str]) -> list[ast.AST]:
    """The parts joined onto a tmp root: `tmp / "a"`, `tmp.joinpath("a")`, `Path(tmp, "a")`."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [node.right] if _is_tmp_expr(node.left, tmp) else []
    if not isinstance(node, ast.Call) or not node.args:
        return []
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "joinpath":
        return list(node.args) if _is_tmp_expr(func.value, tmp) else []
    is_path_builder = _is_join(func) or (isinstance(func, ast.Name) and func.id in PATH_CLASSES)
    if is_path_builder and _is_tmp_expr(node.args[0], tmp):
        return list(node.args[1:])
    return []


# ---- could a probe reach a listed path ------------------------------------------------


def _components(path: str) -> list[str]:
    return [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]


def _match_from(pattern: list[str], entry: list[str], is_dir: bool) -> bool:
    for index, part in enumerate(entry):
        if index >= len(pattern):
            return False
        if pattern[index] == "**":
            return True
        if not fnmatchcase(part, pattern[index]):
            return False
    return len(pattern) == len(entry) or (is_dir and len(pattern) > len(entry))


def could_reach(pattern: str, anywhere: bool, entry) -> bool:
    """Whether a glob-style `pattern` could name `entry`, or anything under it if a dir."""
    pattern_parts = _components(pattern)
    entry_parts = entry.path.split("/")
    starts = range(len(entry_parts)) if anywhere else [0]
    if any(_match_from(pattern_parts, entry_parts[s:], entry.is_dir) for s in starts):
        return True
    # A search from an unknown root with a wildcard name can land inside any directory.
    return bool(
        anywhere and entry.is_dir and pattern_parts and any(ch in pattern_parts[-1] for ch in "*?[")
    )


def _names_entry(value: str, entry) -> bool:
    if not entry.is_dir:
        return entry.path.rsplit("/", 1)[-1] in value
    wanted = entry.path.split("/")
    parts = _components(value)
    return any(parts[i : i + len(wanted)] == wanted for i in range(len(parts)))


# ---- the scan -------------------------------------------------------------------------


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, *FUNCTIONS)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _probe(call: ast.Call, tmp: frozenset[str]) -> tuple[str, str, bool] | str | None:
    """(pattern, kind, anywhere), a reason it cannot be read, or None if not a search."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in WALKERS:
        return None
    receiver = func.value if isinstance(func, ast.Attribute) else None
    if name == "walk" and isinstance(receiver, ast.Name) and receiver.id == "ast":
        return None  # a syntax-tree walk, not a filesystem one
    module_call = isinstance(receiver, ast.Name) and receiver.id in {"glob", "os"}

    if name in {"glob", "iglob", "rglob"}:
        if not call.args and not call.keywords:
            return f"{name}() with no pattern"
        if module_call or receiver is None:
            root, anywhere = ".", False
        else:
            root = _root_of(receiver, tmp)
            if root is _TMP:
                return None
            anywhere = name == "rglob" or root is _UNKNOWN
            root = "." if root is _UNKNOWN else root
        text = _text_of(call.args[0]) if call.args else None
        if text is None:
            return f"{name}() with a pattern that cannot be read"
        pattern = posixpath.join(root, text[0])
        anywhere = anywhere or text[0].startswith(("/", "\\")) or ":" in text[0][:2]
        return pattern, name, anywhere

    # walk / listdir / scandir / iterdir: everything under the root counts as reachable.
    if name == "iterdir":
        root = _root_of(receiver, tmp) if receiver is not None else _UNKNOWN
    elif call.args:
        root = _root_of(call.args[0], tmp)
    else:
        root = "."
    if root is _TMP:
        return None
    if root is _UNKNOWN:
        return f"{name}() on a root that cannot be read"
    return posixpath.join(root, "**"), name, False


def _ls_files_pathspecs(call: ast.Call) -> list[str] | None:
    """Pathspecs after a literal `ls-files`; [] if none; None if this is no such call."""
    strings = [
        node.value
        for arg in call.args
        for node in ast.walk(arg)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    if "ls-files" not in strings:
        return None
    after = strings[strings.index("ls-files") + 1 :]
    return [spec for spec in after if not spec.startswith("-")]


def _markers(source: str) -> dict[int, str]:
    """line -> reason ('' when missing) for each marker comment. Only real comments count."""
    found: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        match = token.type == tokenize.COMMENT and MARKER.match(token.string)
        if match:
            found[token.start[0]] = match.group(1).strip()
    return found


def _flags(tree: ast.AST, entries) -> list[tuple[int, str]]:
    """(line, what) for everything in a parsed file that could be a read of a listed path."""
    flags: list[tuple[int, str]] = []
    skip = _docstring_nodes(tree)
    for node, tmp in _walk_with_tmp(tree, frozenset(), _tmp_fixtures(tree)):
        for part in _tmp_joined_parts(node, tmp):
            skip |= {id(inner) for inner in ast.walk(part) if isinstance(inner, ast.Constant)}
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            flags += [
                (line, f"names {entry.path}{'/**' if entry.is_dir else ''}")
                for entry in entries
                if _names_entry(node.value, entry)
            ]
        if not isinstance(node, ast.Call):
            continue
        specs = _ls_files_pathspecs(node)
        if specs is not None:
            probes = [(spec, "git ls-files", False) for spec in specs] or [
                ("**", "git ls-files", False)
            ]
        else:
            probe = _probe(node, tmp)
            if probe is None:
                continue
            if isinstance(probe, str):
                flags.append((line, probe))
                continue
            probes = [probe]
        for pattern, kind, anywhere in probes:
            flags += [
                (
                    line,
                    f"{kind} {pattern!r} could reach {entry.path}{'/**' if entry.is_dir else ''}",
                )
                for entry in entries
                if could_reach(pattern, anywhere, entry)
            ]
    return flags


def scan_file(path: Path, entries, label: str, honoured: list[str] | None = None) -> list[str]:
    """The problems in one test file.

    A flag on a line carrying a marker with a reason is cleared, and the marker is appended
    to `honoured`. A marker with no reason is itself a problem and clears nothing.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        markers = _markers(source)
    except (SyntaxError, UnicodeDecodeError, tokenize.TokenError) as error:
        return [f"{label}: cannot be parsed ({error}), so it cannot be cleared"]
    problems = [
        (line, "bookkeeping-unread marker without a reason")
        for line, reason in markers.items()
        if not reason
    ]
    cleared: set[int] = set()
    for line, text in _flags(tree, entries):
        if markers.get(line):
            cleared.add(line)
        else:
            problems.append((line, text))
    if honoured is not None:
        honoured += [f"{label}:{line}: {markers[line]}" for line in sorted(cleared)]
    return [f"{label}:{line}: {text}" for line, text in sorted(problems)]


def reader_problems(
    tests_dir: Path, entries, skip: Path | None = None, honoured: list[str] | None = None
) -> list[str]:
    problems: list[str] = []
    for path in sorted(tests_dir.rglob("*.py")):
        if skip is not None and path.resolve() == skip.resolve():
            continue
        label = path.relative_to(tests_dir.parent).as_posix()
        problems += scan_file(path, entries, label, honoured)
    return problems


# ---- the checks -----------------------------------------------------------------------


def _entries():
    if not LIST_PATH.is_file():
        pytest.skip("no .claude/bookkeeping.json: nothing to check")
    return SCOPE.load_list(ROOT)


def test_the_bookkeeping_list_is_valid():
    if not LIST_PATH.is_file():
        pytest.skip("no .claude/bookkeeping.json: nothing to check")
    try:
        SCOPE.load_list(ROOT)
    except SCOPE.ConfigError as error:
        pytest.fail(str(error))


def test_no_test_reads_a_bookkeeping_file(capsys):
    entries = _entries()
    honoured: list[str] = []
    problems = reader_problems(TESTS_DIR, entries, skip=Path(__file__), honoured=honoured)
    audit = ""
    if honoured:
        audit = (
            "\nMarkers honoured (a person's claim that these are not reads; audit them):\n"
            + "\n".join(honoured)
        )
    if not problems and audit:
        with capsys.disabled():  # a passing run must still show what was cleared by hand
            print(audit)
    assert not problems, (
        "A test appears to read a file listed in .claude/bookkeeping.json, so a push that "
        "changes only that file would skip the test that reads it. Drop the path from the "
        "list, or stop the test reading it:\n" + "\n".join(problems) + audit
    )
