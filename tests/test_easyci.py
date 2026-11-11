"""Loads the EasyCI tests from tests/easyci/.

That folder deliberately has no __init__.py: as a package it would be importable as `easyci`
(discovery puts tests/ first on sys.path) and shadow the real package.

Tests built on EasyCITest (each with its own temporary EASYCI_HOME) run in parallel worker
processes; see tests/easyci/parallel.py. EASYCI_TEST_SERIAL=1 runs everything in this process.
"""
import os
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent / 'easyci'


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def load_tests(loader, standard_tests, pattern):
    root = str(HERE.parents[1])
    if root not in sys.path:
        sys.path.insert(1, root)
    if str(HERE) not in sys.path:
        sys.path.insert(1, str(HERE))
    found = unittest.TestLoader().discover(str(HERE), pattern='test_*.py', top_level_dir=str(HERE))
    if os.environ.get('EASYCI_TEST_SERIAL') == '1':
        standard_tests.addTests(found)
        return standard_tests
    from ecsupport import EasyCITest
    from parallel import ParallelSuite
    parallel, serial = ParallelSuite(), unittest.TestSuite()
    for test in _flatten(found):
        heavy = isinstance(test, EasyCITest) and not test.id().startswith('test_integration.')
        (parallel if heavy else serial).addTest(test)
    standard_tests.addTests([serial, parallel])
    return standard_tests
