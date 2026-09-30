"""Whole-package import smoke test.

`make lint` is only `python3 -m py_compile`, which never resolves imports —
a dangling top-level `from src.x import deleted_symbol` compiles cleanly but
breaks the app at startup. This test catches that class of bug by actually
importing the entry point, which transitively imports every sync module.
"""
import importlib


def test_package_imports():
    importlib.import_module("src.main")
