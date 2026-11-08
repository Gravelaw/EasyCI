# Using EasyCI

EasyCI runs the checks in your project's `.github/workflows` on your own Windows PC, keeps each
run alive in the background until it ends, saves tamper-evident evidence, and puts green or red
marks on the commit on GitHub. This page lists every command in plain words. The design is in
[the product spec](spec/PRODUCT.md).

## Install

```powershell
cd D:\EasyCI
python -m pip install -e .
easyci doctor
```

You need Python 3.11 or newer, Git for Windows, and (for GitHub marks) the GitHub CLI logged in
with `gh auth login`. WSL is needed only for jobs that run on a `wsl:` backend. Docker is not
needed. Without installing, `python -m easyci ...` works from this folder.

## The everyday commands

| You want to... | Type | What you get |
|---|---|---|
| Check the PC is ready | `easyci doctor` | A checklist; every problem ends with `Fix: ...` |
| See what would run | `easyci plan` | Jobs, steps, and anything EasyCI cannot run (and why). Nothing is executed |
| Run the checks | `easyci run` | A run id, straight away. The run keeps going after you close the window |
| See how it is going | `easyci status` | One sentence, e.g. "Your tests are running: web (job 1 of 3), step 3 of 8, 4 minutes so far." |
| Wait for the verdict | `easyci wait RUN-ID` | Blocks until the run ends; the exit code is the result |
| Know why it failed | `easyci explain RUN-ID` | The failing job and step, the last lines it printed, and a suggestion |
| Read a log | `easyci logs RUN-ID [--job NAME] [--tail N] [--follow]` | The job's output (step markers start with `##[easyci]`) |
| Stop a run | `easyci cancel RUN-ID` | Stops it within 30 seconds and marks the GitHub checks as `error` |
| See recent runs | `easyci list [--state S] [--limit N]` | One line per run |
| Re-send GitHub marks | `easyci report RUN-ID` | Posts the run's statuses again (use after a network or login problem) |
| Check evidence is untouched | `easyci verify RUN-ID` | Recomputes every sha256; exit 1 and the file name if anything changed |
| Free disk space | `easyci clean [--keep N]` | Deletes old runs beyond the newest N per project (default 20); never the latest passed run of a profile |

`RUN-ID` looks like `20261107-141205-7f3a`. A unique prefix is enough, and `status`, `logs`,
`explain` and `wait` use the latest run of the current repository when you leave it out.

## `easyci run` options

```
easyci run [--repo DIR] [--commit SHA | --ref REF] [--base REF]
           [--profile NAME | --event push|pull_request] [--job NAME ...] [--workflow FILE]
           [--backend native|wsl|wsl:DISTRO] [--config PATH]
           [--wait] [--no-queue] [--no-report] [--keep]
```

- **What is tested** is always a commit, never your working folder: `HEAD` by default. Uncommitted
  changes are ignored. EasyCI copies the commit (and the base) into a bundle and runs in a clean
  clone, so your checkout is never touched.
- `--base` defaults to the merge-base with the default branch. The event defaults to `push` on the
  default branch and `pull_request` elsewhere. A `--profile` from `easyci.yml` sets both the event
  and the job list.
- `--job` may be repeated; jobs they `need` are included automatically.
- `--config PATH` uses an `easyci.yml` kept outside the repository (its sha256 is recorded in the
  run). Otherwise `easyci.yml` is read from the tested commit.
- `--wait` blocks and returns the result exit code. `--no-queue` refuses (exit 5) instead of
  waiting when another run holds the machine. `--no-report` posts nothing to GitHub. `--keep` keeps
  the clean workspace after the run (it is then not part of the sealed evidence).

Only one run uses the PC at a time. Later runs wait in `queued` and start by themselves, oldest
first.

## For AI agents: the JSON contract

Add `--json` to any command. Stdout then holds exactly one JSON document; anything for humans
goes to stderr.

```json
{"schema": "easyci/1", "ok": true, "data": {"run_id": "...", "state": "running", "plain": "...", "jobs": [...]}}
{"schema": "easyci/1", "ok": false, "error": {"code": "BUSY", "message": "...", "fix": "..."}}
```

A typical agent loop:

```powershell
easyci run --repo D:\proj --json            # returns at once with data.run_id
easyci wait 20261107-141205-7f3a --json     # exit 0 passed, 1 failed, 4 unsupported, 6 did not complete
easyci explain 20261107-141205-7f3a --json  # data.summary is safe to quote to a person
```

Branch on `data.state` (`queued`, `preparing`, `running`, `passed`, `failed`, `unsupported`,
`timed_out`, `cancelled`, `interrupted`), not on the exit code of `status`.

| Exit code | Meaning |
|---|---|
| 0 | Success (for `wait` and `run --wait`: the run passed) |
| 1 | The run failed, or `verify` found changed evidence |
| 2 | Usage or configuration error (bad arguments, invalid `easyci.yml`) |
| 3 | Environment problem: missing tool, distro not found, `gh` not logged in |
| 4 | The workflow uses something EasyCI cannot run and nothing overrides it (`plan`, `run`, `wait`) |
| 5 | Machine busy and `--no-queue` given |
| 6 | The run did not complete (`cancelled`, `interrupted`, `timed_out`), or `wait --timeout` ran out |
| 7 | Run id, job, repository or commit not found |
| 10 | Internal error; details in `%USERPROFILE%\EasyCI\easyci.log` |

## What EasyCI understands in a workflow

- `run:` steps with `bash` semantics (Git Bash on `native`, real bash in WSL), `pwsh`,
  `powershell`, `cmd` on `native`; `env`, `working-directory`, `continue-on-error`,
  `timeout-minutes`, `id` with `$GITHUB_OUTPUT`, `$GITHUB_ENV`, `$GITHUB_STEP_SUMMARY`, job
  `outputs`, `needs`.
- `actions/checkout@v3..v5` (clean clone of the tested commit), `actions/setup-node`,
  `setup-python`, `setup-julia`, `setup-java`, `setup-go` (the installed version is **checked**, not
  installed), `actions/upload-artifact@v3/v4`, `actions/cache` (skipped locally).
- Expressions: `github.sha/ref/ref_name/repository/event_name`, the pull-request head and base
  sha, `env.*`, `needs.*.outputs.*`, `needs.*.result`, `steps.*.outputs.*`, `runner.os`, literals,
  `== != && || !`, parentheses, `always() success() failure()`.
- `runs-on: ubuntu-*` runs on the default WSL backend, `windows-*` on `native`; `macos-*` is
  unsupported.

Anything else makes that job `unsupported` with the exact reason (for example
`unsupported_uses:docker/build-push-action@v5`). Nothing of that job is run, and its GitHub check is
`error`. EasyCI never guesses and never counts an unknown step as passed.

EasyCI sets `CI=true`, `EASYCI=1`, `EASYCI_RUN_ID`, `EASYCI_SHA`, `EASYCI_BASE`, `EASYCI_EVENT`,
`EASYCI_PROFILE` and the usual `GITHUB_*`/`RUNNER_*` variables. It does **not** set
`GITHUB_ACTIONS`, because this is not GitHub Actions.

## `easyci.yml`

Optional. Lives at the repository root (or is passed with `--config`).

```yaml
version: 1
backend: wsl:my-distro        # where ubuntu-* jobs run (native, wsl, wsl:<distro>)
report: true                  # post GitHub commit statuses
retention: 20                 # runs kept per project by `easyci clean`
gpu: optional                 # or required: nvidia-smi must exist on the backend
profiles:
  pr:   {event: pull_request, jobs: [web, julia]}
jobs:
  some-gate: {skip: true}     # never run this job
  web:
    when: always              # ignore the workflow's `if:`
    backend: native
    timeout_minutes: 360      # hard limit; the job is stopped and marked timed_out
    stall_minutes: 30         # no output this long marks the run "stalled"
    stall_action: mark        # or kill: stop the job as failed (reason stalled)
    command: npm test         # replace every step of the job with this (Git Bash on native)
    on_kill: ./release.sh     # runs if EasyCI stops the job (cancel, timeout, stall kill); see below
    on_kill_minutes: 10       # time limit for on_kill
    skip_steps: [3, "Upload coverage"]   # by 1-based index or by name
    evidence: [reports/**]    # copied into the run's evidence after the job
    required_env: [MY_TOKEN]  # host variables passed through (and masked as *** in logs)
    toolchains: {node: ignore}           # do not check this setup-* version
```

`on_kill` is for jobs that start things outside EasyCI's own process tree, such as a delegated
runner that sets up a database or swap file inside WSL. When EasyCI stops a step because of a
cancel, a timeout or `stall_action: kill`, it kills the step's process tree first and then runs
`on_kill` in the same workspace with the same environment, plus `EASYCI_KILL_REASON` (`cancelled`,
`timed_out`, `step_timeout` or `stalled`). That happens before evidence is collected and before
the workspace is removed. It never runs when a job passes or fails on its own. Its output goes to
the job log, its exit code is recorded as `on_kill` on the job in `easyci status --json`, and it
never changes the job's result. Every step also sees `EASYCI_JOB` (the job id).

A worked example for JuliaFlow is in [`examples/juliaflow/easyci.yml`](../examples/juliaflow/easyci.yml).

## Where things are kept

Everything lives under `%USERPROFILE%\EasyCI` (set `EASYCI_HOME` to move it), never inside your
repository. Each run has its own folder `projects\<project>\runs\<run-id>\` with `run.json`
(state), `heartbeat.json` (rewritten every 10 seconds while the run is alive), `plan.json`,
`report.json` (results and the reproducibility record), one folder per job with `log.txt` and
artifacts, and `sha256.json`, written last, which seals the run.

## When things go wrong

- **"The run was interrupted"**: the PC restarted or the EasyCI process was killed. The next
  `easyci` command notices within about a minute, marks the run `interrupted`, releases the
  machine, and sets its GitHub checks to `error`. Start it again with `easyci run`.
- **"Reported to GitHub: not yet"**: posting failed (offline, logged out). Fix the login and run
  `easyci report RUN-ID`; the run itself is unaffected.
- **Stalled**: a job printed nothing for `stall_minutes`. By default that is only a label (Julia
  precompilation is often silent for a long time); `timeout_minutes` is what stops a job.
