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
python -m unittest discover -s tests     # EasyCI (fake gh, temp EASYCI_HOME) plus the legacy tests
python scripts/check-static.py           # legacy Jenkins scaffold policy checks
```

Real WSL and `gh` checks are opt-in: set `EASYCI_INTEGRATION=1` (and `EASYCI_TEST_DISTRO` for WSL
runs).

Tests that start real runs (each with its own temporary `EASYCI_HOME`) are spread over worker
processes, so the suite finishes in well under a minute. `EASYCI_TEST_WORKERS=N` sets the number of
workers and `EASYCI_TEST_SERIAL=1` runs everything in one process.

## Legacy: Jenkins scaffold (frozen)

The Jenkins scaffold below is **frozen legacy** (spec section 4): it is kept for reference with its
tests and evidence, is no longer maintained, and is off the critical path. Its files
(`compose*.yaml`, `controller/`, `agent/`, `projects/`, `schemas/`, `pipelines/`, `scripts/`,
`evidence/`, `dependency-lock.json`, `docs/setup.md` and the other `docs/*.md` pages) stay where they
are for now and move to `legacy/jenkins/` in Phase 1. `scripts/ci.py`'s snapshot logic lives on,
re-implemented, inside `easyci/snapshot.py`.

This task-owned scaffold has passed static verification, sixteen host tests,
both image builds and independent checks of all 37 plugin hashes. On 2026-10-02
the approved controller started at localhost:8080 with named storage. The user
completed administrator setup; the final local-admin matrix policy is active.
The exclusive one-executor Linux demo agent ran build #2 successfully, including
user review and identical-byte local promotion. See evidence/live-demo.json.
JuliaFlow native Windows/WSL integration and wider operational qualification
remain pending. No
administrator, enrollment secret, API token, service account, OAuth grant,
hosted CI or GitHub synchronization was provisioned by the assistant. Jenkins
created its own first-run unlock secret; the assistant did not access it. The
source JuliaFlow CI and profiling checkouts were read only.

The controller runs in a Linux container with durable named storage and no build
executors. A separate Linux container agent handles the demo. JuliaFlow uses a
separately approved native Windows agent to call its existing Node/WSL runner;
the Linux agent has no Docker socket, host drive, WSL, GPU, or implicit host access.
Jenkins schedules jobs and review gates; the existing runner owns validation and
local package delivery.

Files:

- `compose.yaml`, `controller/`: localhost controller, JCasC, pinned plugins.
- `agent/`: optional unprivileged inbound Linux agent over WebSocket.
- `projects/`, `schemas/`, `pipelines/`: reusable contracts and generated UI pipeline scripts.
- `scripts/ci.py`: trusted snapshot ingestion, adapter execution and exact-byte promotion.
- `tests/`: real Git snapshots, demo delivery and rejection-path tests.
- [Setup](docs/setup.md), [operations](docs/operations.md), [platforms](docs/platforms.md),
  [security](docs/security.md), [qualification](docs/qualification.md),
  [activation approvals](docs/activation-approvals.md).
- [Initial evidence](evidence/verification.json) and [2026-10-02 follow-up](evidence/followup-2026-10-02.json):
  exact commands, results and open runtime gates. The follow-up qualifies actual
  Compose rendering, 16 host tests and the final local-admin-only matrix policy.

Run bounded local checks using existing Python, Git, PyYAML and jsonschema:

```powershell
python scripts/check-static.py
python -m unittest discover -s tests -v
```

The runtime adapter needs Python 3.11+ and Git; its core has no pip dependencies.
The static checker needs PyYAML and jsonschema, already present on this task host.
No installation has been performed. Pipeline scripts are administrator-installed
in the Jenkins UI with Groovy sandbox enabled. There is no automatic seed job or
SCM checkout, and no local-checkout protection override.

Docker Desktop 4.92.0 is already installed outside PATH; no reinstall is currently
indicated. Its launch is approved and remains pending the release-gate owner's
safe-window confirmation. First-run
credential entry/submission belongs to the user. Use the temporary bootstrap
override for the wizard, then switch to default final JCasC before adding jobs or
agents; ordinary authenticated users receive no permissions in that final policy.

The user selected the dedicated private shared infrastructure repository
`Gravelaw/EasyCI`. Its reviewed scaffold is intended for development branch
`codex/jenkins-scaffold`, with each product owning its adapter contract and a
pinned infra release. Keep general Jenkins operations out of JuliaFlow product
source. Repository publication is authorized; merges, deployment, image builds,
controller startup and enrollment remain separate decisions.

The controller baseline is Jenkins 2.580.1 / Java 21, verified against the
[official LTS changelog](https://www.jenkins.io/changelog-stable/). Official
[JCasC](https://plugins.jenkins.io/configuration-as-code/),
[agent](https://github.com/jenkinsci/docker-agents/blob/master/README_inbound-agent.md),
and [lock](https://plugins.jenkins.io/lockable-resources/) documentation informed
the scaffold. Compatibility with the exact pinned plugin closure still needs a
live startup test. See dependency-lock.json for digests and download checksums.
