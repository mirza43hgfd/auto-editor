"""Static import-integrity test (Qt-free).

Every intra-package `from .mod import name` must reference a name that exists
at the top level of `mod`. Catches stale imports like the v2.0 bug where
pipeline_panel imported MOTION_CYCLE after it was deleted from auto_pipeline.
Pure AST parsing — no module imports, so it runs anywhere (no Qt needed).
"""

import ast
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.normpath(os.path.join(HERE, "..", "auto_editor"))


def _names_in(stmts):
    names = set()
    for node in stmts:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Tuple):
                    names.update(e.id for e in t.elts if isinstance(e, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Try):
            names |= _names_in(node.body + node.orelse)
            for h in node.handlers:
                names |= _names_in(h.body)
            names |= _names_in(node.finalbody)
        elif isinstance(node, ast.If):
            names |= _names_in(node.body + node.orelse)
    return names


def _top_level_names(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    return _names_in(tree.body)


def test_intra_package_imports_resolve():
    py_files = sorted(
        f for f in os.listdir(PKG) if f.endswith(".py") and f != "__init__.py"
    )
    available = {f[:-3]: _top_level_names(os.path.join(PKG, f)) for f in py_files}
    errors = []
    for f in py_files:
        path = os.path.join(PKG, f)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = None
            if node.level and node.level > 0:
                mod = (node.module or "").split(".")[-1] or None
                # `from . import x` -> names are submodules
                if node.module is None:
                    for a in node.names:
                        if a.name != "*" and a.name not in available:
                            errors.append(f"{f}: from . import {a.name} (no such module)")
                    continue
            elif (node.module or "").startswith("auto_editor."):
                mod = node.module.split(".")[-1]
            if mod is None or mod not in available:
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                if a.name not in available[mod]:
                    errors.append(
                        f"{f}: cannot import name {a.name!r} from .{mod} "
                        f"(stale import?)"
                    )
    assert not errors, "stale intra-package imports:\n" + "\n".join(errors)


if __name__ == "__main__":
    test_intra_package_imports_resolve()
    print("IMPORT INTEGRITY OK")
