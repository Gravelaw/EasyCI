# EasyCI

EasyCI is local-first CI for one developer on one Windows PC. It reads the GitHub Actions
workflows your project already has (`.github/workflows/*.yml`), runs them on your own machine
(inside WSL or directly on Windows), keeps every run alive in a detached background supervisor
until it ends, writes tamper-evident evidence to disk, and posts the result back onto the commit
on GitHub so pull requests show green or red checks again. No server, no Docker, no new secret:
it is one Python command, `easyci`, driven by AI agents through `--json` and by people through
plain sentences.

- What and why: [product spec](docs/spec/PRODUCT.md) (section 14 is the MVP scope).
- Every command in plain words: [docs/usage.md](docs/usage.md).
- New to CI/CD? [The plain-language guide](docs/guide/ci-explained.md).

## Install

```powershell
cd D:\EasyCI
python -m pip install -e .
easyci doctor
```

Needs Python 3.11+ (with PyYAML), Git for Windows and, for GitHub checks, the GitHub CLI logged
in (`gh auth login`). WSL is needed only for `wsl:` backends. Without installing,
`python -m easyci ...` works from this folder.

## Quickstart for people

```powershell
cd D:\my-project
easyci doctor        # is this PC ready? every problem comes with "Fix: ..."
easyci plan          # what would run, and anything EasyCI cannot run (nothing is executed)
easyci run           # starts in the background; you can close the window
easyci status        # "Your tests are running: web (job 1 of 2), step 3 of 8, 4 minutes so far."
easyci explain       # after a failure: which step, its last lines, and what to try
```

## Quickstart for agents

```powershell
easyci run --repo D:\my-project --json          # returns at once: data.run_id, data.state, data.launch
easyci wait <run-id> --json                       # exit 0 passed, 1 failed, 4 unsupported, 6 incomplete
easyci explain <run-id> --json                    # data.summary is safe to quote to the owner
```

Every `--json` document is `{"schema": "easyci/1", "ok": ..., "data" | "error": ...}` and is
the only thing on stdout; errors carry `code`, `message` and `fix`. The run keeps going after the
agent session ends. See [docs/usage.md](docs/usage.md) for exit codes and options.

JuliaFlow: `easyci run --repo "D:\Julia Project" --profile pr --config examples\juliaflow\easyci.yml`
delegates each PR job to JuliaFlow's own `scripts/ci/wsl.mjs` (see
[`examples/juliaflow/easyci.yml`](examples/juliaflow/easyci.yml)).

## Tests

```powershell
python -m unittest discover -s tests     # EasyCI (fake gh, temp EASYCI_HOME)
```

Real WSL and `gh` checks are opt-in: set `EASYCI_INTEGRATION=1` (and `EASYCI_TEST_DISTRO` for WSL
runs).

Tests that start real runs (each with its own temporary `EASYCI_HOME`) are spread over worker
processes, so the suite finishes in well under a minute. `EASYCI_TEST_WORKERS=N` sets the number of
workers and `EASYCI_TEST_SERIAL=1` runs everything in one process.

## Legacy: Jenkins scaffold (frozen)

The earlier Jenkins scaffold is frozen and lives in [`legacy/jenkins/`](legacy/jenkins/README.md),
with its own tests and evidence. Nothing in EasyCI uses it, and its tests are not part of the
default suite.
