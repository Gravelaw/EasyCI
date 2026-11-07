# EasyCI product specification (draft 2)

Date: 2026-11-07. Status: reviewed draft, awaiting the owner's approval. Drafted with a Fable
design agent, then reviewed against the code it cites. Inputs: the EasyCI audit and landscape
research note (2026-11-07), the existing Jenkins scaffold in this repository, and JuliaFlow's
`.github/workflows/ci.yml`, `scripts/ci/workflows.json` and `scripts/ci/wsl.mjs` (read only).

EasyCI is a local-first CI/CD tool for one developer on one Windows PC. It reads the GitHub
Actions workflows a project already has, runs them on the machine the developer already owns
(inside WSL or directly on Windows), keeps every run alive and watched by a detached supervisor
until it ends, writes tamper-evident evidence to disk, and posts the result back onto the commit
on GitHub so pull requests show green or red checks again. It is a single Python command,
`easyci`, that an AI coding agent can drive through JSON and that a non-programmer can drive
through plain sentences.

---

## 1. The problem

JuliaFlow/LowcodeML is a private GitHub repository whose hosted Actions jobs stopped starting the
moment the monthly minute quota ran out, because the spending limit is zero. A PR profile for
this project costs roughly two to four runner-hours per push (web ~15 min, Julia 30-60 min cold,
release gate 60-180 min), so the free quota of a private repo lasts about a week. The owner
switched to running the suite locally and now has two half-solutions:

1. A Jenkins scaffold in this repository. It is carefully hardened, but it cannot currently
   start on this PC (Docker Desktop is broken, Java is absent), and even when it did run, every
   build needed a person to create a snapshot, paste a 64-character hash into a form and click
   approve. The JuliaFlow adapter has never executed. Nothing reports back to GitHub.
2. JuliaFlow's own WSL runner (`scripts/ci/wsl.mjs`). It works technically and has achieved one
   full cold PR pass, but it dies whenever the terminal, tool session or scheduled task that
   launched it ends (Windows `0xC000013A` control-C exits), leaving `summary.json` stuck at
   `running` forever. It has no heartbeat, no stall detection, no queue, hardcodes one distro
   and one project, duplicates the workflow definition in `workflows.json`, and never reports to
   GitHub, so PRs show only skipped jobs. A run is stuck in `running` right now.

The root causes the product must remove are: (a) runner lifetime tied to the caller's session;
(b) no supervisor that notices stuck or orphaned runs; (c) a second hand-maintained copy of the
workflow; (d) results invisible on GitHub; (e) everything hardcoded to one project and distro;
and (f) a tooling surface (Docker, Java, Jenkins admin, JCasC, Groovy) that neither an AI agent
nor a non-DevOps person can operate.

## 2. Target users and personas

| Persona | Who | What they need from EasyCI | What they must never need |
|---|---|---|---|
| **The agent** (Claude Code, Codex) | Writes code in the repo, pushes a branch, wants to know whether checks pass before asking the owner to merge | A CLI with `--json` on every command, stable exit codes, a run id it can poll, a plain-language failure summary it can quote, and the guarantee that a run it started keeps going after its session ends | Knowledge of Jenkins, Docker, WSL internals; interactive prompts; secrets |
| **The owner** (Akshay, today) | Builds JuliaFlow, not DevOps-literate, one Windows 11 Home PC with WSL2 | A "run my checks" button or one command; a status sentence in English ("Your tests are running: step 3 of 8, 12 minutes so far"); `easyci doctor` that says exactly what is wrong and how to fix it; green/red marks on the PR | Editing YAML, reading stack traces, administering a server |
| **The small team** (later, 2-5 people) | Same repo, maybe a second machine | Shared conventions in `easyci.yml`, the same evidence format on every machine, statuses that say which machine ran what | A central server they have to host and secure |

The agent persona is primary for the MVP: it is the user that actually types the commands
today. The owner persona drives the second phase (wizard, launcher, dashboard). The team
persona only constrains design choices (nothing machine-specific in the repo, honest platform
labels), it does not get features in the MVP.

## 3. Positioning: why not an existing tool

The research note compared the landscape on 2026-11-07. Summary of why each option is not the
answer for this owner, and what EasyCI borrows from it:

| Option | Why not, for this owner | What EasyCI takes from it |
|---|---|---|
| **GitHub-hosted runners** | Private repo, 2-4 runner-hours per push, $0 spending limit: jobs silently never start. Even with a limit raised, $7-15/week for a solo project is money spent to wait on someone else's machine | The workflow YAML itself; the commit-status contract |
| **Self-hosted `actions/runner`** | Runs the real YAML, but the runner service must stay online, needs a registration token (a new secret), runs fork-PR code on the PC if misconfigured, and GitHub has already proposed (then postponed) a per-minute fee on self-hosted minutes for private repos. The control plane is still GitHub's; a future billing change breaks it again | Run the real YAML, not a copy |
| **nektos/act** | Needs Docker (broken here); images are approximations; services differ; recent CVE in its cache server. Good for "does my YAML parse", not a trustworthy gate | Read `.github/workflows` directly |
| **Jenkins** (the current scaffold) | Most powerful, but a Java controller, plugin upkeep, Groovy, an admin UI, and manual hash-pasting. Cannot run on this PC today. Not agent- or beginner-friendly. See section 4 | Commit-pinned snapshots, tree identity, sha256 inventories, fail-closed locks, never-overwrite promotion, localhost-only |
| **Woodpecker / Drone** | Server plus agents, container-native, need Docker and a forge webhook reaching the PC (public exposure). Drone CE is stagnant | Simple YAML pipeline model |
| **Gitea / Forgejo Actions** | Actions-compatible YAML, but requires hosting a forge and its act-based runner is Linux-container focused | Compatibility target for the YAML subset |
| **Dagger / Earthly** | Pipelines-as-code on a container engine; need Docker/Podman; neither schedules nor reports. Earthly's company wound down in 2025 | Nothing in the MVP; Dagger is a possible Phase 4 backend |
| **Buildkite** | Good hybrid (your agents, their control plane) but a paid per-user SaaS | The agent/control-plane split, kept local |
| **pre-commit / lefthook** | Seconds-long hooks; cannot run three-hour suites | Complementary; EasyCI does not replace them |
| **DVC / MLflow / CML** | Data and experiment tooling, not a CI engine | `easyci-metrics.json`, artifact store outside Git, data hashes in the reproducibility record |

EasyCI's position: **the control plane is a file tree on your own disk, the engine is one Python
process, the workflow definition is the one GitHub already reads, and the only external contact
is posting a status on your own commit through the `gh` login you already have.** No server, no
new secret, no inbound network, no Docker requirement.

## 4. Decision: Jenkins comes off the critical path

**Decision: replace, not keep.** The Jenkins scaffold is frozen as legacy; EasyCI is a new,
small engine. The scaffold is not deleted: in Phase 1 it moves to `legacy/jenkins/` with its
tests and evidence, its README gets a one-paragraph status note, and nothing in it is maintained
further.

Reasons:

1. It cannot run here. The controller needs Docker Desktop (broken, reinstall blocked) and the
   JuliaFlow agent needs Java 21 on Windows (absent). The JuliaFlow adapter has never executed.
2. The operating model is manual by design: snapshot, hash, paste, approve. That is the opposite
   of what an agent needs (start, poll, read JSON) and of what a non-programmer needs (one button).
3. Operating cost. 37 pinned plugins plus 57 unreviewed ones, JCasC, matrix auth, Groovy sandbox,
   compose files, lock files: all of it is surface area for one person who does not want to be a
   Jenkins admin.
4. Jenkins adds nothing EasyCI needs for one machine. Its strengths (fleets of agents, plugin
   ecosystem, pipeline DSL) are not on the requirements list. Its weaknesses (JVM, UI-first,
   state inside the controller) are exactly the pain.

What survives, as design rules rather than code: commit-pinned git-bundle snapshots with a
verified tree id; a sha256 inventory written last over every evidence file; a fail-closed
directory lock for exclusive operations; promotion that never overwrites; no arbitrary command
from untrusted parameters; localhost-only exposure for anything that listens. `scripts/ci.py`'s
`ingest`/`prepare` logic is the reference for the snapshot step and will be re-implemented inside
the engine, with credit, not imported.

## 5. Architecture

### 5.1 Components

EasyCI is one Python package (`easyci/`, Python 3.11+, stdlib plus PyYAML) exposing one console
script. Internally:

| Component | Responsibility |
|---|---|
| **CLI** (`easyci/cli.py`) | Parses commands, prints plain text or `--json`, maps outcomes to exit codes. Never does long work itself |
| **Importer** (`easyci/workflow.py`) | Reads `.github/workflows/*.yml` plus optional `easyci.yml`, produces a *plan*: ordered jobs, resolved steps, backend per job, and a list of anything unsupported |
| **Supervisor** (`easyci/supervisor.py`) | A detached process, one per run. Takes the queue lock, snapshots the commit, executes jobs through a backend, streams logs, heartbeats, enforces timeouts and stall limits, reports to GitHub, finalises evidence. The only process that writes `run.json` |
| **Backends** (`easyci/backends/{wsl,native,docker}.py`) | Turn "run this script with this env in this directory" into a process on a platform; probe toolchain versions; kill a process tree; translate paths |
| **Reporter** (`easyci/github.py`) | Posts commit statuses via `gh api`; idempotent; queues failures for `easyci report` to retry |
| **Store** (`easyci/store.py`) | On-disk layout, run ids, queue lock, stale-run reaper, sha256 inventory, retention |
| **Explainer** (`easyci/explain.py`) | Turns a finished run into a plain-language summary by reading the last failing step's tail and known error patterns |

There is no daemon. Each `easyci` invocation is short-lived except the supervisor, which lives
exactly as long as its run.

### 5.2 Run lifecycle

A run is created by `easyci run`. The CLI writes `request.json`, spawns the supervisor detached,
waits up to five seconds for the first heartbeat, and returns the run id. Everything after that
is the supervisor's.

```
                 easyci run
                     |
                     v
   +----------+   lock   +-----------+  snapshot  +---------+
   |  queued  | -------> | preparing | ---------> | running | --+--> passed
   +----------+          +-----------+            +---------+   +--> failed
        |                      |                     |  ^        +--> unsupported
        |                      |                     |  |        +--> timed_out
        |                      |                     v  |        +--> cancelled
        |                      |               (stalled flag)    +--> interrupted
        |                      |                     |
        +----------------------+---------------------+---- any live state whose
                                                           heartbeat goes stale and
                                                           whose supervisor PID is dead
                                                           is reaped to: interrupted
```

| State | Meaning | Who sets it |
|---|---|---|
| `queued` | Request written; waiting for the queue lock | CLI (initial), supervisor while waiting |
| `preparing` | Lock held; bundling the commit, cloning the clean workspace, probing toolchains | Supervisor |
| `running` | At least one job executing. Sub-fields: current job, current step, `stalled: true/false` | Supervisor |
| `passed` | Every selected job passed | Supervisor |
| `failed` | At least one job's step exited non-zero (or a stall was killed) | Supervisor |
| `unsupported` | The plan contained something EasyCI cannot honestly run and no override covers it. Nothing was executed for that job | Supervisor (or CLI, before spawning, if the whole plan is unsupported) |
| `timed_out` | A job exceeded its `timeout-minutes`; process tree killed | Supervisor |
| `cancelled` | `easyci cancel` was called; process tree killed | Supervisor, on request from CLI |
| `interrupted` | Supervisor died (power loss, `taskkill`, crash). Set by the reaper, never by the supervisor | Any later CLI invocation (reaper) |

Terminal states are the last six. A run in a terminal state is never modified again except for
`report.json.github` (status reporting retries) and `sha256.json` (written once, last).

`stalled` is a flag on `running`, not a state. A job is stalled when it has produced no output
for `stall_minutes` (default 20). The default action is to mark and keep waiting; the hard
`timeout-minutes` is what kills. A project can set `stall_action: kill` per job, in which case the
job ends `failed` with reason `stalled`.

Heartbeat: the supervisor rewrites `heartbeat.json` (`{"at", "pid", "phase", "job", "step"}`)
every 10 seconds. A heartbeat older than 60 seconds whose `pid` is not alive means the run is
orphaned. The reaper runs at the start of every CLI command (and in `easyci status`), marks such
runs `interrupted`, posts `error` statuses for their pending GitHub contexts, and releases the
queue lock if that run held it. A run is therefore never left at `running` for longer than one
CLI invocation after its supervisor dies.

Queue: one run at a time per machine (the PC has one set of CPUs and one WSL). The lock is a
directory `queue.lock/` containing `owner.json` (run id, pid, taken-at). `mkdir` is the atomic
primitive, as in `ci.py`. A second `easyci run` either waits in `queued` (default) or exits
immediately with code 5 (`--no-queue`). Queued runs start in the order they were created.

Sleep prevention: while a run is `preparing` or `running`, the supervisor holds
`SetThreadExecutionState(ES_SYSTEM_REQUIRED | ES_CONTINUOUS)` so Windows does not sleep in the
middle of a three-hour job. Display sleep is not prevented.

### 5.3 Supervisor detachment on Windows

This is the single most important mechanism and the one the JuliaFlow runner got wrong. The
supervisor must survive: the launching console closing; the Claude Code or Codex session that ran
the command ending; the parent `python` or `node` process being killed; a Task Scheduler task
ending. Rules:

1. Spawn with `creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
   CREATE_BREAKAWAY_FROM_JOB`, `close_fds=True`, stdin/stdout/stderr redirected to files under the
   run directory. No console means no `CTRL_CLOSE_EVENT`, which is what produced the
   `0xC000013A` exits.
2. If breakaway is refused (the parent is inside a Job object that forbids it, which some agent
   harnesses do), fall back to launching through `schtasks /create /sc once /st <now+1min> /f`
   with no execution time limit and `/ru` the current user, then `schtasks /run`. The CLI reports
   which launch path was used in `run.json.launch`.
3. The supervisor installs a console control handler that ignores `CTRL_C`/`CTRL_BREAK`, and
   handles cancellation only through the `cancel` file in its run directory.
4. The supervisor re-execs itself with `-I` (isolated mode) and an explicit interpreter path so
   it does not depend on the caller's environment, cwd or `PYTHONPATH`.
5. Acceptance is literal: close the terminal that launched the run; the heartbeat must keep
   updating (criterion 3 in section 14).

### 5.4 On-disk layout

Everything lives under `EASYCI_HOME`, default `%USERPROFILE%\EasyCI` (visible in Explorer,
outside every repository, as `ci.py` insisted). Evidence is never written inside the source repo.

```
%USERPROFILE%\EasyCI\
  config.yml                      global defaults (backend, reporting on/off, retention)
  easyci.log                      engine log (CLI + supervisors, rotated at 10 MB)
  queue.lock\owner.json           present only while a run holds the machine
  projects\<slug>\                slug = repo dir name + 6 hex of its absolute path
    project.json                  repo path, remote, default branch, last import summary
    runs\<run-id>\                run-id = YYYYMMDD-HHMMSS-xxxx (sortable, typeable)
      request.json                what was asked: commit, base, profile/event, backend, options
      run.json                    authoritative state (section 5.2); rewritten atomically
      heartbeat.json              rewritten every 10 s while live
      supervisor.pid / supervisor.out / supervisor.err
      cancel                      created by `easyci cancel`; supervisor polls it
      source.bundle               commit-pinned bundle (commit + base); tree id in run.json
      plan.json                   the imported plan, including every mapping decision
      workspace\                  clean clone for native backend (deleted after run unless --keep)
      jobs\<job>\
        job.json                  status, timestamps, steps[], exit codes, reason
        log.txt                   combined stdout/stderr with step markers, redacted
        artifacts\<name>\         from upload-artifact or easyci.yml `evidence:` globs
        metrics.json              copy of easyci-metrics.json if the job wrote one
      report.json                 final summary: result, durations, toolchains, lockfile hashes,
                                  github statuses posted, reproducibility record
      sha256.json                 inventory of every file above, written last; run is sealed
  cache\<backend>\                opt-in per-backend caches (Phase 2)
  artifacts\<project>\<run-id>\   large artifact store (Phase 3)
```

For the `wsl` backend the clean clone lives *inside the distro's own filesystem*
(`~/.easyci/work/<run-id>/`), not under `/mnt/d/...`. npm and Julia on a 9P-mounted Windows drive
are several times slower; JuliaFlow's runner already clones into ext4 for that reason. Logs are
streamed back to the Windows run directory; the WSL workspace is removed at the end unless
`--keep`.

## 6. Agent interface

Every command accepts `--json`. In JSON mode stdout contains exactly one JSON document and
nothing else; human text goes to stderr. Every JSON document has `"schema": "easyci/1"`.

### 6.1 Commands

| Command | Does | Returns (JSON `data`) |
|---|---|---|
| `easyci run [--repo DIR] [--commit SHA\|--ref REF] [--base REF] [--profile NAME \| --event push\|pull_request] [--job NAME ...] [--backend ID] [--wait] [--no-queue] [--no-report] [--keep]` | Creates a run and starts the detached supervisor. Default repo: cwd. Default commit: `HEAD`. Default event: `pull_request` if the ref is not the default branch, else `push`. Returns immediately unless `--wait` | run object |
| *(defaults for `run`)* | `--base` defaults to `git merge-base <commit> origin/<default branch>`; `--profile` from `easyci.yml` wins over `--event` | |
| `easyci status [RUN-ID] [--watch]` | State of one run (default: the latest for this repo). Runs the reaper first | run object |
| `easyci list [--repo DIR] [--limit N] [--state S]` | Recent runs | `runs: [run object...]` |
| `easyci logs RUN-ID [--job NAME] [--tail N] [--follow]` | Job log text | `{job, lines: [...], truncated}` |
| `easyci wait RUN-ID [--timeout MIN]` | Blocks until terminal state; exit code reflects result | run object |
| `easyci cancel RUN-ID` | Requests cancellation; waits up to 30 s for the tree to die | run object |
| `easyci explain RUN-ID` | Plain-language failure summary | `{summary, failing_job, failing_step, excerpt, suggestions[]}` |
| `easyci plan [--repo DIR] [--profile NAME]` | Imports the workflow without running; shows jobs, steps, mapping decisions, unsupported items | plan object |
| `easyci doctor [--repo DIR]` | Checks the host and (if given) the repo; every problem has a `fix` string | `{checks: [{id, ok, detail, fix}]}` |
| `easyci report RUN-ID` | Re-posts GitHub statuses for a run whose reporting failed | `{posted: [...], failed: [...]}` |
| `easyci verify RUN-ID` | Recomputes `sha256.json`; exit 1 on any mismatch | `{ok, mismatches: [...]}` |
| `easyci clean [--keep N]` | Deletes evidence of runs beyond retention (default keep 20 per project, never the latest passed) | `{deleted: [...]}` |
| `easyci setup` | Guided wizard (Phase 2) | n/a |

### 6.2 JSON shapes

Run object (returned by `run`, `status`, `wait`, `cancel`, and inside `list`):

```json
{
  "schema": "easyci/1",
  "ok": true,
  "data": {
    "run_id": "20261107-141205-7f3a",
    "project": "Julia Project-9c1e2b",
    "repo": "D:\\Julia Project",
    "commit": "846a025...", "tree": "b1c4...", "base": "f912cc7...",
    "event": "pull_request", "profile": "pr",
    "state": "running", "stalled": false,
    "launch": "detached",
    "created_at": "2026-11-07T14:12:05Z", "started_at": "...", "finished_at": null,
    "elapsed_s": 744,
    "jobs": [
      {"name": "web", "state": "passed", "backend": "wsl:juliaflow-ci-ubuntu24",
       "steps_total": 8, "steps_done": 8, "elapsed_s": 612, "exit_code": 0, "reason": null},
      {"name": "julia", "state": "running", "steps_total": 2, "steps_done": 1,
       "elapsed_s": 132, "last_output_at": "...", "stalled": false},
      {"name": "debian-release-gate", "state": "pending"}
    ],
    "github": {"reported": true, "contexts": {"easyci": "pending", "easyci/web": "success"}},
    "paths": {"run_dir": "C:\\Users\\aksha\\EasyCI\\projects\\...\\runs\\20261107-141205-7f3a"},
    "plain": "Running: julia (job 2 of 3), step 2 of 2, 12 minutes so far."
  }
}
```

Error document (any command):

```json
{"schema": "easyci/1", "ok": false,
 "error": {"code": "GH_NOT_LOGGED_IN", "message": "gh is installed but not logged in.",
           "fix": "Run: gh auth login   (or pass --no-report to run without GitHub statuses)"}}
```

Job states: `pending`, `running`, `passed`, `failed`, `unsupported`, `skipped` (condition
false), `timed_out`, `cancelled`, `interrupted`. Job `reason` is a short machine string when
not passed (`step_failed`, `stalled`, `unsupported_uses:docker/build-push-action@v5`,
`toolchain_mismatch:node 26.3.0 != 25`, `timeout`, `cancelled`).

### 6.3 Exit codes

| Code | Meaning | Typical command |
|---|---|---|
| 0 | Success; for `wait`/`run --wait`: the run passed | all |
| 1 | The run failed (a step exited non-zero, or a stall was killed) | `wait`, `run --wait`, `verify` |
| 2 | Usage or configuration error (bad arguments, invalid `easyci.yml`) | all |
| 3 | Environment problem: missing tool, distro not found, gh not logged in when reporting is on | `doctor`, `run` |
| 4 | Unsupported workflow content and no override | `plan`, `run`, `wait` |
| 5 | Machine busy and `--no-queue` given | `run` |
| 6 | Run did not complete: `cancelled`, `interrupted`, `timed_out` | `wait`, `run --wait` |
| 7 | Run id or repo not found | `status`, `logs`, `cancel`, ... |
| 10 | Internal error (bug); details in `easyci.log` | all |

`status`, `list`, `logs`, `plan` exit 0 whenever they could answer; the result is in the data.
Agents should branch on `data.state`, not on the exit code of `status`.

An MCP server wrapper (`easyci mcp`) exposing the same commands as tools is Phase 2; the JSON
shapes above are its contract, so nothing changes for agents already using the CLI.

## 7. Non-programmer interface

The same engine, different words. Principles: never show a stack trace by default; every problem
comes with a fix sentence; one command (or one double-click) does the common thing.

| Need | MVP | Phase 2 |
|---|---|---|
| "Is my computer ready?" | `easyci doctor`: a checklist in English. "Git: found. GitHub login: found (aksha). WSL distro juliaflow-ci-ubuntu24: found, Node 25.9.0, Julia 1.12.7. Docker: not found (not needed)." Each failed line ends with "Fix: ..." | `easyci setup` wizard that runs doctor, finds the repo and workflows, picks the backend, writes `easyci.yml`, and offers to run once |
| "Run my checks" | `easyci run` in the repo folder | `Run checks.cmd` on the Desktop (double-click: runs `easyci run --wait` in a window that stays open and ends with a one-line verdict); localhost dashboard with one big button |
| "What is happening?" | `easyci status` prints one sentence: "Your tests are running: julia (job 2 of 3), step 2 of 2, 12 minutes so far." Stalled: "Still running but nothing printed for 23 minutes; this can be normal for Julia, EasyCI will stop it at 6 hours if it never finishes." | Dashboard shows the same sentence plus the live log |
| "Why did it fail?" | `easyci explain`: "The job *web* failed at step 5 of 8 (`npm run test`). The last lines say: ... . Suggestion: run `npm run test` in the project and look at the first red line." | Same text in a PR comment |
| "Did it get to GitHub?" | Status sentence includes "Reported to GitHub: yes (3 checks)" or "not yet; run `easyci report`" | Dashboard link to the commit |

State-to-sentence mapping used by `status` and the dashboard:

| State | Sentence |
|---|---|
| `queued` | "Waiting for the previous run to finish (it is on job X)." |
| `preparing` | "Getting a clean copy of your code ready." |
| `running` | "Your tests are running: JOB (job i of n), step j of k, M minutes so far." |
| `passed` | "All checks passed in M minutes. GitHub shows green on commit abc1234." |
| `failed` | "Checks failed: JOB stopped at step j (`command`). Run `easyci explain` for details." |
| `unsupported` | "EasyCI could not run JOB because the workflow uses ACTION, which EasyCI does not support yet. Nothing was run for that job." |
| `timed_out` | "JOB ran longer than N minutes and was stopped." |
| `cancelled` | "Stopped because you asked." |
| `interrupted` | "The run was interrupted (the computer may have restarted or the process was killed). Start it again with `easyci run`." |

## 8. Workflow reuse: reading `.github/workflows`

EasyCI reads the project's real workflow files at the commit being tested (from the bundle, not
the working tree). There is no second copy to keep in sync; JuliaFlow's `workflows.json` drift
check becomes unnecessary once JuliaFlow runs through the importer rather than its own runner.

### 8.1 Import rules

1. Every `*.yml`/`*.yaml` under `.github/workflows/` is parsed with PyYAML (safe loader). The
   `on:` block selects which workflows apply to the requested event (`push` or `pull_request`,
   with `branches` filters honoured; `workflow_dispatch` workflows are selectable by name only).
2. Jobs are ordered by `needs:` (topological; cycles are a config error, exit 2). Jobs run
   serially in the MVP; a job whose dependency did not pass is `skipped`.
3. `runs-on` selects the backend: `ubuntu-*` -> the default `wsl:` distro, `windows-*` ->
   `native`, `macos-*` -> `unsupported`. `easyci.yml` can override per job.
4. Each `run:` step becomes a script executed by the backend with GitHub's default shell
   semantics: `bash --noprofile --norc -eo pipefail` for Linux and for `shell: bash`;
   `pwsh`/`powershell` on native when declared; `cmd` for `shell: cmd`. `env:` merges
   workflow -> job -> step. `working-directory` is honoured. `continue-on-error: true` is
   honoured. `timeout-minutes` is honoured (default 360, as on GitHub).
5. Step `id` plus `$GITHUB_OUTPUT` and `$GITHUB_ENV` files are supported so `steps.<id>.outputs`
   and job `outputs:` work. `$GITHUB_STEP_SUMMARY` is captured into evidence.
6. The standard variables are set: `CI=true`, `GITHUB_ACTIONS` is **not** set (EasyCI is not
   Actions; code that checks it should see the truth), `EASYCI=1`, `EASYCI_RUN_ID`,
   `EASYCI_SHA`, `EASYCI_BASE`, `EASYCI_EVENT`, `GITHUB_SHA`, `GITHUB_REF`, `GITHUB_REF_NAME`,
   `GITHUB_REPOSITORY`, `GITHUB_WORKSPACE`, `GITHUB_EVENT_NAME`, `RUNNER_OS`, `RUNNER_TEMP`.
7. Anything the importer does not understand makes the *job* `unsupported` with a precise reason.
   It is never skipped as a pass and never guessed.

### 8.2 `uses:` mapping

| Action | EasyCI behaviour |
|---|---|
| `actions/checkout@v3..v5` | Clean clone of the exact run commit from the bundle into the workspace (`path:` honoured). `ref:` must be absent or resolve to the run commit; otherwise unsupported. `fetch-depth` ignored (the bundle has the commit and base). `submodules: true` unsupported |
| `actions/setup-node`, `actions/setup-python`, `julia-actions/setup-julia`, `actions/setup-java`, `actions/setup-go` | Toolchain **verification**, not installation. The backend is probed (`node --version` etc.); the requested version must match by major (or exactly when the request is exact, e.g. `1.12.7`). Mismatch -> job unsupported with reason `toolchain_mismatch:...` and a fix sentence naming the distro. `cache:` inputs ignored |
| `actions/upload-artifact@v3/v4` | Copies `path:` globs into `jobs/<job>/artifacts/<name>/`; `if-no-files-found: error` honoured; `if: always()` honoured |
| `actions/download-artifact` | Phase 1 (copy from an earlier job in the same run). MVP: unsupported |
| `actions/cache` | MVP: no-op with a log line "cache step skipped (local)". Phase 2: local cache dir keyed by the `key:` expression |
| anything else | `unsupported`, reason names the action. A project can declare a step safe to skip (`skip_steps`) or replace the whole job (`command`), see 8.4 |

### 8.3 `${{ }}` expression subset

Supported in `if:`, `with:`, `env:`, `run:` and `name:`:
`github.sha`, `github.ref`, `github.ref_name`, `github.repository`, `github.event_name`,
`github.event.pull_request.head.sha` (equals the run commit), `github.event.pull_request.base.sha`,
`env.X`, `needs.<job>.outputs.<k>`, `needs.<job>.result`, `steps.<id>.outputs.<k>`,
`matrix.<k>` (Phase 1, see below), `runner.os`, string/number/boolean literals, `==`, `!=`,
`&&`, `||`, `!`, parentheses, and the functions `always()`, `success()`, `failure()`.

Everything else (`contains`, `startsWith`, `format`, `toJSON`, `fromJSON`, `hashFiles`,
`secrets.*`, `vars.*`, `inputs.*`, `github.actor`, ...) makes the step, and therefore the job,
`unsupported`, with the expression text in the reason. Phase 1 adds `contains`, `startsWith`,
`endsWith`, `hashFiles` and simple `strategy.matrix` list expansion; `include`/`exclude` and
`fromJSON` matrices stay out.

### 8.4 `easyci.yml` (project-level overrides)

Optional in general; **required for JuliaFlow**, because its workflow's `execution-policy` job
emits `enabled=false` when hosted execution is off and every other job is gated on it. A literal
import would honestly skip everything. The file lives at the repo root and is read from the
tested commit.

```yaml
version: 1
backend: wsl:juliaflow-ci-ubuntu24      # default for ubuntu-* jobs
report: true                            # post commit statuses
profiles:                               # named selections; `--profile` picks one
  pr:   {event: pull_request, jobs: [web, julia, debian-release-gate]}
  push: {event: push,         jobs: [web, julia]}
jobs:
  execution-policy:
    skip: true                          # hosted-only gate; EasyCI decides locally
  web:
    when: always                        # ignore the workflow `if:` for this job
    timeout_minutes: 360
    stall_minutes: 30
  julia:
    when: always
    stall_minutes: 45                   # Pkg.instantiate and precompile are quiet
  debian-release-gate:
    when: always
    timeout_minutes: 180
    # Delegate to JuliaFlow's proven runner instead of translating the steps.
    # The runner itself drives WSL, so EasyCI runs it on the Windows host.
    backend: native
    command: node scripts/ci/wsl.mjs --ref $EASYCI_SHA --base $EASYCI_BASE --profile pr --job debian-release-gate --cache cold
    evidence: [artifacts/local-ci/*/summary.json, artifacts/local-ci/*/sha256.json, artifacts/local-ci/*/debian-release-gate/**]
    required_env: []
```

Override keys per job: `skip`, `when: always|workflow`, `backend`, `command` (replaces every step;
the job's `uses:` are not evaluated), `timeout_minutes`, `stall_minutes`, `stall_action:
mark|kill`, `skip_steps: [name or index]`, `evidence: [globs copied into the job's artifacts]`,
`required_env: [names that must exist on the host and are passed through]`, `toolchains:
{node: ignore}`. Top-level: `backend`, `report`, `profiles`, `retention`, `gpu: required|optional`.

A delegated `command` on the `native` backend runs inside EasyCI's own clean clone of the exact
commit (under the run directory on a Windows drive), never in the developer's checkout. That
matters for `wsl.mjs`: it compares its own runner files with the selected commit, requires its
evidence path to be on a WSL-mounted Windows drive, and writes `artifacts/local-ci/` into the clone
it runs from. `command` is executed with the job's shell (Git Bash on `native`), so `$EASYCI_*`
variables expand.

For the MVP acceptance run (section 14) all three JuliaFlow jobs use `command:` delegation to
`wsl.mjs --job <name>`, exactly as `debian-release-gate` above. That exercises the supervisor,
queue, heartbeat, evidence and GitHub reporting end to end while reusing the runner that already
passed once. Phase 1 replaces `web` and `julia` with direct translation through the `wsl` backend
(their steps are plain `npm` and `julia` commands) and keeps the release gate delegated until
EasyCI has a PostgreSQL service story.

## 9. Execution backends

| Backend id | MVP | How a step runs | Toolchain probe | Kill | Platform label in evidence |
|---|---|---|---|---|---|
| `wsl:<distro>` | yes | `wsl -d <distro> [-u user] --exec setsid bash <script>` with the workspace on ext4 inside the distro; the script writes its PGID to the job dir; output streamed to `log.txt` | `/etc/os-release`, `bash`, `git`, `node`, `npm`, `python3`, `julia`, `pg_config` (each if present) | `kill -TERM -- -<pgid>`, then `-KILL` after 15 s; `wsl.exe` child killed with `taskkill /T` | `wsl:<distro>:ubuntu-24.04` |
| `native` | yes | Git for Windows `bash.exe` for bash steps (same semantics as GitHub's default), `pwsh`/`powershell`/`cmd` when declared; workspace under the run dir | `git`, `node`, `npm`, `python`, `julia`, `pwsh` | `taskkill /T /F /PID` on the step process | `windows-11-home:native` |
| `docker:<image>` | Phase 3 | Container per job with the workspace bind-mounted; services from `services:` | image labels | `docker rm -f` | `docker:<image@digest>` |

Common contract (`Backend` protocol): `probe() -> dict`, `prepare(run) -> workspace`,
`exec(step, env, cwd, timeout, on_output) -> exit_code`, `kill(job)`, `cleanup(run)`,
`to_backend_path(win_path)`. Backends never decide pass/fail; the supervisor does.

Default user for `wsl` is the distro's default user. JuliaFlow's delegated runner runs as root
inside its dedicated distro by its own choice; EasyCI does not change that and records the user
in evidence. `easyci doctor` warns when the default user of the chosen distro is root.

GPU: `gpu: required` in `easyci.yml` makes `doctor` and `preparing` check `nvidia-smi` inside
the backend and mark the run `unsupported` with a fix if absent. No scheduling of GPUs beyond
the one-at-a-time queue.

## 10. GitHub integration

MVP: commit statuses only, through the user's existing `gh` login. No new token, no GitHub App,
no webhook, no inbound port.

- Contexts: `easyci` (overall) and `easyci/<job>` per selected job. Both can be required checks
  in branch protection or rulesets (for private repositories that needs GitHub Pro or a paid
  organisation plan; without it the statuses are still shown, just not enforced).
- Transitions: all contexts set to `pending` when the run leaves `queued` ("EasyCI: queued on
  AKSHAY-PC"); each job context to `success`/`failure` when the job ends; `error` for
  `unsupported`, `timed_out`, `cancelled`, `interrupted`; the overall context last.
- Description (GitHub limit 140 chars): `"passed in 14m 02s · run 20261107-141205-7f3a ·
  wsl:juliaflow-ci-ubuntu24"` or `"failed at step 5/8 npm run test · run ... "`. No
  `target_url` in the MVP (there is nothing to link to); the dashboard URL is added in Phase 2.
- Call: `gh api repos/{owner}/{repo}/statuses/{sha} -f state=... -f context=... -f
  description=...`. Owner/repo come from the `origin` remote. Statuses are posted only for the
  exact commit whose tree was tested (the bundle's commit), never for a working tree.
- Failure to post never fails the run. The outcome is recorded in `report.json.github` and
  `easyci report RUN-ID` retries. `--no-report` or `report: false` disables posting; `doctor`
  checks `gh auth status` and the token's `repo` scope.
- Hosted-skip policy stays the project's business. JuliaFlow's `execution-policy` job keeps
  making hosted Actions skip; EasyCI's statuses sit next to those skipped jobs with real results.

Later (Phase 2-3): `easyci watch` polls `gh pr list`/`gh api` every N minutes for new heads on
open PRs of the user's own branches and queues runs (no inbound network, no fork PRs); a PR
comment with the `explain` text and metrics; an optional GitHub App for Check Runs with
annotations, only if the owner wants richer output than statuses give.

## 11. Security model

- **Trusted code only.** EasyCI runs the owner's own repositories and branches. It never fetches
  fork PRs, never runs code from a remote it did not already have locally, and `watch` only
  queues heads of branches in the same repository. The `ci.py` rule "no arbitrary command from
  job parameters" becomes: commands come from the workflow files and `easyci.yml` *at the tested
  commit*, never from CLI arguments or GitHub API responses.
- **No secrets stored.** EasyCI holds no tokens; `gh` holds the GitHub credential. `secrets.*`
  in a workflow is unsupported. `required_env` passes named host variables through and nothing
  else; the environment a step sees is constructed, not inherited.
- **Redaction.** Values of passed-through variables whose names match `*TOKEN*`, `*SECRET*`,
  `*PASSWORD*`, `*KEY*`, plus the output of `gh auth token`, are replaced by `***` in logs before
  they touch disk.
- **Localhost only.** The Phase 2 dashboard binds `127.0.0.1` with a per-start random path
  token. Nothing else listens.
- **Evidence integrity.** Runs are sealed with `sha256.json`, files are set read-only, and
  `easyci verify` detects tampering. This proves byte identity, not trust, as `ci.py` says.
- **Git hygiene.** Git is invoked with `GIT_CONFIG_NOSYSTEM=1`, global config pointed at
  `nul`, `core.hooksPath=` and `credential.helper=` cleared, `GIT_TERMINAL_PROMPT=0`. The
  bundle is cloned with `--no-hardlinks`; tracked symlinks and submodules are rejected.
- **Isolation is per-run, not per-user.** Steps run with the user's privileges (or the distro's
  user). EasyCI is not a sandbox; that is the trusted-code assumption made explicit.

## 12. Evidence and artifacts

A run directory is the complete record. `report.json` contains: result and per-job results with
durations and exit codes; the plan with every mapping decision; backend ids and platform labels;
toolchain versions as probed; the **reproducibility record** (commit, tree, base, sha256 of each
workflow file and of `easyci.yml` at the commit, sha256 of `package-lock.json`, `Project.toml`,
`Manifest.toml`, `requirements*.txt`, `uv.lock`, `poetry.lock` when present, EasyCI version,
host name, declared data hashes in Phase 3); GitHub statuses posted with timestamps; launch path.

Rules: evidence is written once and sealed; retention keeps the last 20 runs per project and
always the latest passed run per profile; `easyci clean` is the only deleter; nothing in a repo
working tree is ever modified by a run (the delegated JuliaFlow runner writes to its own
`artifacts/local-ci/`, which EasyCI copies via `evidence:` globs and then leaves alone).

Large artifacts (models, datasets, built packages over a configurable size) go to
`artifacts\<project>\<run-id>\` in Phase 3 with a pointer and hash in `report.json`, keeping run
directories small and the layout DVC-friendly (a DVC remote can point at that folder).

Promotion (copying a passed release candidate into a durable, never-overwritten release folder)
is Phase 3, re-implemented from `ci.py`'s `promote` with its fail-closed lock. Until then
JuliaFlow's `npm run cd:local -- --run <id>` remains the project's own step.

## 13. Data science and Julia support

Long jobs are the normal case, not the exception: default timeout 6 hours, stall default 20
minutes with per-job overrides, sleep prevention, and a supervisor that outlives any session.
Julia specifics the importer and backends know about: `Pkg.instantiate` and precompilation are
quiet for many minutes (so the `julia` stall default in the wizard is 45 minutes); `JULIA_DEPOT_PATH`
inside the distro is recorded in the toolchain probe; `Project.toml`/`Manifest.toml` hashes are in
the reproducibility record; `julia-actions/setup-julia` is verified, not installed.

Metrics: a job may write `easyci-metrics.json` (flat object of numbers and strings, max 64 KB)
anywhere under the workspace. The supervisor copies it to `jobs/<job>/metrics.json`, `status`
shows the keys ("accuracy 0.934, train_time_s 1812"), and Phase 2 posts them in the PR comment
with the delta against the latest passed run on the base branch.

GPU and data: `gpu: required|optional` (section 9); `data:` list in `easyci.yml` of paths or
globs whose sha256 (or DVC `.dvc` file hash) goes into the reproducibility record (Phase 3).

## 14. MVP scope and acceptance criteria

Scope of one focused build session: the package skeleton and CLI; the supervisor with detach,
heartbeat, queue, stall, timeout, cancel and reaper; importer for `run:` steps, checkout,
setup-* verification, upload-artifact, cache no-op, the expression subset, `easyci.yml`
overrides; `wsl` and `native` backends; GitHub commit statuses via `gh`; `status` in JSON and
plain English; `doctor`; `explain` (basic: last failing step tail); evidence with `sha256.json`
and `verify`; unit tests with fake backends; one opt-in integration test.

Acceptance criteria. Each is a test or a scripted check; the MVP ships when all pass.

1. **Doctor.** On this host `easyci doctor --json` exits 0 and reports `python`, `git`, `gh`
   (logged in), `wsl` with the three distros, and `docker: absent (optional)`. With `gh` logged
   out it exits 3 and the `gh` check carries `fix: "Run: gh auth login ..."`.
2. **Start returns fast.** `easyci run --repo "D:\Julia Project" --profile pr --json` returns a
   run id within 10 seconds, with `state` in `{queued, preparing, running}` and `launch` set.
3. **Survives the launcher.** Start a run from a fresh `cmd.exe` window and close the window;
   start another from a Claude Code session and end the session. In both cases, 60 seconds later
   `heartbeat.json.at` is less than 30 seconds old and `state` is still live. (Checked by an
   integration script; also tested with a stub job of `sleep 120`.)
4. **Unattended JuliaFlow PR profile.** With the `easyci.yml` of section 8.4, a run of profile
   `pr` on a current JuliaFlow commit reaches a terminal state without any human action, is never
   observed at `running` after its supervisor has exited, and ends `passed` or `failed` with
   per-job results that match the delegated runner's `summary.json`.
5. **Statuses on the commit.** After criterion 4, `gh api repos/.../commits/<sha>/status` shows
   contexts `easyci`, `easyci/web`, `easyci/julia`, `easyci/debian-release-gate`, each
   `success`/`failure` (not `pending`), with descriptions containing the run id.
6. **Orphan reaping.** Kill a live supervisor with `taskkill /F /PID`. The next
   `easyci status` (at most 90 seconds later) reports `interrupted`, the GitHub contexts that
   were `pending` are `error`, and `queue.lock` is gone.
7. **Queue.** A second `easyci run` while one is live shows `queued`, starts automatically when
   the first ends, and `--no-queue` exits 5 with `error.code = BUSY`.
8. **Stall.** A stub job that prints once then sleeps 600 s, with `stall_minutes: 1`, shows
   `stalled: true` within 2 minutes and keeps running; with `stall_action: kill` the job ends
   `failed` with `reason: stalled` within 2 minutes and the process tree is gone.
9. **Timeout.** The same stub with `timeout_minutes: 1` ends `timed_out` within 90 seconds, the
   WSL process group no longer exists (`wsl -d X --exec pgrep -g <pgid>` is empty), and `wait`
   exits 6.
10. **Cancel.** `easyci cancel` on a live run yields `cancelled` within 30 seconds with no
    leftover processes in the backend, and the GitHub contexts are `error`.
11. **Plan import.** `easyci plan --repo "D:\Julia Project" --event pull_request --json`
    without `easyci.yml` lists `execution-policy` (1 run step), `web` (8 run steps, checkout
    mapped, setup-node verified), `julia` (2 run steps, setup-julia verified),
    `debian-release-gate` (3 run steps + upload-artifact mapped, `if:` evaluated with
    `github.event_name == 'pull_request'`), and marks `web`/`julia`/`debian-release-gate` as
    `skipped` by condition (the honest literal reading). With `easyci.yml` they are selected.
12. **Unsupported is honest.** A fixture workflow using `docker/build-push-action@v5` yields job
    state `unsupported`, `reason` naming the action, exit 4 from `wait`, GitHub context `error`,
    and no step of that job executed.
13. **Toolchain verification.** A fixture with `setup-node` `node-version: 25` on the `native`
    backend (host has Node 26) yields `unsupported` with `reason: toolchain_mismatch:node ...`
    and a fix sentence; on `wsl:juliaflow-ci-ubuntu24` (Node 25.9.0) it passes verification.
14. **Evidence sealed.** Every terminal run has `sha256.json` covering every file in the run
    directory except itself; `easyci verify` exits 0; after appending one byte to a log it
    exits 1 and names the file.
15. **JSON discipline.** For every command, `--json` output parses as one JSON document, has
    `schema: easyci/1`, and nothing else is written to stdout; errors have `error.code`,
    `message`, `fix`. Checked by a test that runs each command in success and failure mode.
16. **Plain status.** `easyci status` on a live run prints exactly one line matching
    `^(Your tests are running|Waiting|Getting a clean copy|All checks passed|Checks failed|...)`
    and includes elapsed minutes.
17. **Redaction.** A stub step that echoes `$MY_TOKEN` (passed via `required_env`) produces
    `***` in `log.txt` and the raw value appears nowhere under the run directory.
18. **Tests.** `python -m unittest discover -s tests` passes in under 60 seconds on a machine
    without WSL or `gh` (backends and `gh` are faked); `EASYCI_INTEGRATION=1` enables the real
    WSL/gh tests behind criteria 3-10.

## 15. Non-goals

- Not a general GitHub Actions emulator: no marketplace actions beyond the mapped set, no
  `services:` in the MVP, no containers in the MVP, no `secrets`, no reusable workflows
  (`uses: ./.github/workflows/x.yml`), no composite actions.
- Not a server, not multi-machine scheduling, not a fleet. One machine, one run at a time.
- Not a sandbox for untrusted code. No fork PRs, ever, automatically.
- Not a replacement for `pre-commit`-style hooks or for the project's own test runners.
- Not a release/deploy tool in the MVP (promotion is Phase 3; deployment stays out).
- Not a Jenkins migration tool; the scaffold is frozen, not converted.
- No GUI in the MVP; no GitHub App; no webhooks; no cloud runners.

## 16. Roadmap

| Phase | Goal | Contents | Exit condition |
|---|---|---|---|
| **MVP** | JuliaFlow PR profile runs unattended and reports to the PR | Section 14 | Criteria 1-18 pass; three consecutive unattended PR runs on real commits |
| **Phase 1: Native import** | Run `web` and `julia` by translating their steps through the `wsl` backend, no delegation | `download-artifact`, simple matrices, `contains`/`startsWith`/`hashFiles`, local workspace caches for npm and the Julia depot, `skip_steps`; move Jenkins scaffold to `legacy/jenkins/`; retire JuliaFlow's `workflows.json` parity check for those two jobs | `web` and `julia` results identical to delegated runs on the same commit across 5 commits; wall time not worse than delegated |
| **Phase 2: Humans and agents** | Non-programmer and agent ergonomics | `easyci setup` wizard, Desktop launcher, localhost dashboard (status sentence, live log, "Run checks" button, run history), `easyci watch` polling for new PR heads, PR comment with `explain` text and metrics delta, `easyci mcp` server, `target_url` in statuses | Owner runs and reads a result with no terminal; an agent uses only MCP tools for a full cycle |
| **Phase 3: Data science and releases** | Long-running, reproducible, promotable | `docker:` backend (if Docker is repaired) with `services:` for PostgreSQL so the release gate no longer needs delegation; artifact store with size threshold; `data:` hashes; GPU check; `promote` with fail-closed lock and never-overwrite; metrics comparison across runs | Release-candidate profile runs natively and promotes into a durable folder with a verified receipt |
| **Phase 4: Team** | Second machine, richer GitHub | Shared `easyci.yml` conventions, machine labels in contexts (`easyci/web@machine`), optional GitHub App for Check Runs with annotations, Dagger or Podman backend evaluation | Two machines report on the same PR without conflicting |

## 17. Risks and open questions

Risks:

| Risk | Impact | Mitigation |
|---|---|---|
| Windows detachment still loses the supervisor in some harness (Job objects forbidding breakaway) | The core promise fails | Two launch paths (detached, `schtasks`), both covered by criterion 3; `launch` recorded so failures are diagnosable |
| Stall false positives on quiet Julia phases | Alarming status, or a killed good run if `kill` is set | Default `mark`, generous per-job defaults, wizard sets 45 min for Julia jobs |
| Semantic drift from real Actions (shell flags, env, expression edge cases) | "Passes locally, fails hosted" or vice versa | Small explicit subset; `unsupported` instead of guesses; hosted Actions can be re-enabled for a final check on a release branch |
| `gh` token scope or expiry | No statuses | `doctor` checks scope; posting failures never fail the run; `easyci report` retries |
| WSL I/O on `/mnt` | Jobs several times slower | Workspace on ext4 inside the distro; only logs cross the boundary |
| Disk growth from bundles and logs | Full drive | Retention 20 runs per project, `easyci clean`, size shown in `list` |
| Docker stays broken | No `docker:` backend, release gate stays delegated | Phase 3 is gated on Docker health; nothing in MVP needs it |
| Single PC: sleep, reboots, updates | Interrupted runs | Sleep prevention; reaper marks `interrupted` and the status says to re-run; `watch` can auto-requeue in Phase 2 |
| Someone starts JuliaFlow's runner by hand while EasyCI also runs | Two heavy suites compete for RAM and the same distro; the EasyCI queue cannot see the manual run | `doctor` and `preparing` look for live `jfci-*` summaries (`status: running` with a live process) and warn; a project may declare `busy_paths` globs that make EasyCI wait |
| Legacy Jenkins scaffold rots in the repo | Confusion for future agents | Moved under `legacy/`, README says it is frozen, no tests run by default |

Open questions for the owner:

1. `EASYCI_HOME` default: `%USERPROFILE%\EasyCI` (visible) or `%LOCALAPPDATA%\EasyCI` (hidden)?
   This spec assumes visible.
2. Should `easyci run` default to `--wait` for humans (blocking window with a verdict) and
   detached for `--json`? This spec says detached always, with `--wait` opt-in, so behaviour never
   depends on who is calling.
3. Retention default of 20 runs: is that enough given JuliaFlow's bundles (~tens of MB each)?
4. Should `easyci watch` (Phase 2) auto-run every push to every branch, or only PR heads?
5. Is a PR comment (needs no extra scope beyond `repo`) wanted, or are statuses enough until the
   dashboard exists?
6. The `release-candidate` profile and `delivery.mjs` promotion: keep delegated until Phase 3, or
   bring `promote` forward?
7. Machine name in status descriptions: fine to expose `AKSHAY-PC` on a private repo?

Reviewer's recommended answers (used for the build unless the owner says otherwise): 1 visible
`%USERPROFILE%\EasyCI`; 2 detached always, `--wait` opt-in, and the Phase 2 Desktop launcher uses
`--wait`; 3 keep 20 runs, revisit after measuring; 4 PR heads only; 5 statuses only for now;
6 keep promotion delegated to `delivery.mjs` until Phase 3; 7 yes, the machine name is shown
(private repo, and it makes "which PC ran this" obvious once there are two).

## 18. Where this spec diverges from the proposed direction

The proposed direction was adopted in full on: Jenkins off the critical path and frozen as
legacy; a Python stdlib+PyYAML single-CLI engine; detached supervisor with heartbeat, stall
detection, one-at-a-time queue and reaper; direct reading of `.github/workflows` with honest
`unsupported`; `wsl`/`native` backends with `docker` later; commit statuses through `gh`; `--json`
everywhere; plain-language status and doctor; trusted-code-only security; evidence sealing; the
data-science items; JuliaFlow as first adopter. Divergences, each with the reason:

1. **Stall does not kill by default.** Proposed: stall -> `stalled`, optional kill. Here the
   default is mark-only and the hard timeout is the only default killer. Julia precompilation
   and long test suites are legitimately silent for 10-30 minutes; an unattended run that gets
   killed for being quiet is worse than one that is labelled "quiet". `stall_action: kill` exists
   for projects that want it.
2. **`easyci.yml` is required for JuliaFlow, not optional.** Its `execution-policy` gate makes
   a literal import skip every job. The spec keeps the literal reading honest (criterion 11) and
   makes `skip: true` + `when: always` the documented fix, rather than teaching the importer a
   JuliaFlow-specific exception.
3. **Delegation is per job, via `wsl.mjs --job <name>`, not one profile-level override.** The
   runner already supports `--job`; per-job delegation gives one GitHub context per job, per-job
   timeouts and stalls, and per-job evidence without parsing another tool's summary to split
   results. The cost (three bundles, three preflights) is seconds against hours of tests.
4. **Detachment has two launch paths.** `DETACHED_PROCESS` alone is not enough when the caller
   sits inside a Job object that kills children on close, which agent harnesses commonly do.
   `CREATE_BREAKAWAY_FROM_JOB` with a `schtasks` fallback is specified, and criterion 3 tests
   closing a real console and a real agent session, not just `Popen` returning.
5. **Sleep prevention is in the supervisor.** Not in the proposal, but a 3-hour job on a
   Windows 11 Home laptop-class PC that goes to sleep is the next most likely way to get a stuck
   `running`. One `ctypes` call.
6. **The WSL workspace lives inside the distro, not on `/mnt`.** Performance, and the JuliaFlow
   runner already does this; the proposal did not say.
7. **`GITHUB_ACTIONS` is deliberately not set.** Code that branches on it should learn it is on
   EasyCI (`EASYCI=1`), consistent with honest platform labels.
8. **Matrix expansion moves to Phase 1.** The expression evaluator knows `matrix.*`, but
   expansion with `include`/`exclude` rules is more code than the MVP needs; JuliaFlow has no
   matrices.
9. **`verify`, `report`, `wait` and `plan` are added to the command list.** `wait` gives agents a
   blocking call with result exit codes without making `status` lie with its exit code; `plan`
   lets an agent see what will happen before spending hours; `report` and `verify` make the
   GitHub and evidence promises checkable.
