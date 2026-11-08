"""Supervisor entry point, run as `python -I _launch.py <run-dir>`.

-I drops PYTHONPATH and the script directory from sys.path, so add the package root explicitly.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Task Scheduler launches get a fresh environment; restore the caller's EASYCI_* settings.
_env_file = pathlib.Path(sys.argv[1]) / 'launch-env.json' if len(sys.argv) > 1 else None
if _env_file and _env_file.is_file():
    for _key, _value in json.loads(_env_file.read_text(encoding='utf-8')).items():
        if _key.startswith('EASYCI_'):
            os.environ.setdefault(_key, _value)

from easyci.supervisor import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
