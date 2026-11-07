"""Supervisor entry point, run as `python -I _launch.py <run-dir>`.

-I drops PYTHONPATH and the script directory from sys.path, so add the package root explicitly.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from easyci.supervisor import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
