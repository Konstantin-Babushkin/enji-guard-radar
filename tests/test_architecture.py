"""Core modules stay pure: no I/O imports, no shell imports, no clock reads, no file I/O.

Everything not listed in SHELL_MODULES is core, so new modules are covered without edits.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

PACKAGE = "enji_guard_radar"
SRC = Path(__file__).resolve().parents[1] / "src" / PACKAGE

# Modules that may do I/O. telegram, state_store and settings arrive in plans 01-04 and 01-06.
# env_file reads a file, so it is a shell module even though the plan's list omits it.
SHELL_MODULES = {
    "cli",
    "env_file",
    "fixture_source",
    "telegram",
    "state_store",
    "settings",
    "guard_credentials",
    # cli_auth and mcp_guard arrive in plans 02-05 and 02-06; listing them early keeps those
    # two plans from both editing this file. A name with no module has no effect on the checks.
    "cli_auth",
    "mcp_guard",
    "cli_doctor",
    "cli_probe",
}
# `__init__` only imports `cli` lazily inside main(), so the package import stays light.
EXEMPT_MODULES = {"__init__"}
FORBIDDEN_CORE_IMPORTS = {"socket", "urllib", "http", "fcntl", "subprocess", "ssl", "time"}
# Pure string work: URL building and parsing do no I/O, so core may use exactly this one.
ALLOWED_CORE_IMPORTS = {"urllib.parse"}
CLOCK_CALLS = {"now", "utcnow", "today"}
FILE_IO_CALLS = {
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "mkdir",
    "unlink",
    "touch",
    "open",
}


def core_modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SRC.glob("*.py")):
        if path.stem in SHELL_MODULES or path.stem in EXEMPT_MODULES:
            continue
        yield path.stem, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_names(tree: ast.Module) -> Iterator[str]:
    """Every dotted name a module imports, relative imports resolved against the package."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level > 0:
                base = f"{PACKAGE}.{base}" if base else PACKAGE
            yield base
            for alias in node.names:
                yield f"{base}.{alias.name}"


def called_names(tree: ast.Module) -> Iterator[str]:
    """Names of called functions and methods, e.g. `open` and `read_text`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                yield node.func.id
            elif isinstance(node.func, ast.Attribute):
                yield node.func.attr


def forbidden_imports(tree: ast.Module) -> Iterator[str]:
    """Imports a core module may not have: I/O libraries and shell modules."""
    for imported in imported_names(tree):
        if any(imported == ok or imported.startswith(f"{ok}.") for ok in ALLOWED_CORE_IMPORTS):
            continue
        parts = imported.split(".")
        if parts[0] in FORBIDDEN_CORE_IMPORTS:
            yield imported
        elif parts[0] == PACKAGE and len(parts) > 1 and parts[1] in SHELL_MODULES:
            yield f"shell module {imported}"


def test_core_modules_do_not_import_io_or_shell_modules() -> None:
    violations = [
        f"{name} imports {imported}"
        for name, tree in core_modules()
        for imported in forbidden_imports(tree)
    ]
    assert violations == []


def test_only_urllib_parse_is_allowed_in_core() -> None:
    request = ast.parse("import urllib.request")
    parse = ast.parse("from urllib.parse import urlencode")
    bare = ast.parse("import urllib")
    assert "urllib.request" in set(forbidden_imports(request))
    assert set(forbidden_imports(bare)) == {"urllib"}
    assert list(forbidden_imports(parse)) == []


def test_core_modules_do_not_read_the_system_clock() -> None:
    violations = [
        f"{name} calls {called}()"
        for name, tree in core_modules()
        for called in called_names(tree)
        if called in CLOCK_CALLS
    ]
    assert violations == []


def test_core_modules_do_no_file_io() -> None:
    violations = [
        f"{name} calls {called}()"
        for name, tree in core_modules()
        for called in called_names(tree)
        if called in FILE_IO_CALLS
    ]
    assert violations == []
