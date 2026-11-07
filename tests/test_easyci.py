"""Loads the EasyCI tests from tests/easyci/.

That folder deliberately has no __init__.py: as a package it would be importable as `easyci`
(discovery puts tests/ first on sys.path) and shadow the real package.
"""
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent / 'easyci'


def load_tests(loader, standard_tests, pattern):
    root = str(HERE.parents[1])
    if root not in sys.path:
        sys.path.insert(1, root)
    standard_tests.addTests(unittest.TestLoader().discover(str(HERE), pattern='test_*.py', top_level_dir=str(HERE)))
    return standard_tests
