# Adopting EasyCI in another repository

A checklist for bringing a second project under EasyCI after JuliaFlow. The step-by-step version for
non-programmers is [getting-started.md](getting-started.md); this page is the short list and the
decisions to make along the way.

## Once per PC

1. Python 3.11 or newer, then `python -m pip install --user -e D:\EasyCI`.
2. Git for Windows (EasyCI uses its Git Bash for `native` jobs).
3. GitHub CLI, then `gh auth login` with the `repo` scope. Each person reports with their own login.
4. Optional: WSL, if the project's tests should run on Linux. A dedicated distro per project (as
   JuliaFlow has `juliaflow-ci-ubuntu24`) keeps toolchains and services apart.
5. `python -m easyci doctor` says what is missing and how to fix it.

## Once per project

1. **Look at the project's CI first.** Read `.github/workflows/*.yml` and any runner scripts the
   project already has. If the project runs its tests through its own script, decide between:
   - **native** (recommended): EasyCI runs the workflow's own steps. Use `setup:`/`cleanup:` for
     whatever the project's runner prepares (memory settings, swap, a wrapper on `$GITHUB_PATH`) and
     `env:` for the variables it sets. JuliaFlow went this way, and its own runner is being retired
     (`examples/juliaflow/easyci.yml`).
   - **delegating**: `command:` in `easyci.yml` calls that script. Same results as today and least
     work at first, but the project keeps two runners in step
     (`legacy/juliaflow/easyci.delegated.yml` is the old JuliaFlow example).
2. **Run `python -m easyci setup --repo <folder>`.** It detects Node, Python, Julia, Go, Rust,
   Java and .NET, picks a backend that has the tools, and shows what it would write. Nothing is written
   until you add `--write` (or `--commit`), and it never overwrites a file.
3. **Plan before running:** `python -m easyci plan --repo <folder>`. Every job must say what it
   will do. Anything marked unsupported needs `skip_steps`, `command:`, `skip: true` or a backend
   change. EasyCI never pretends to run something it cannot.
4. **Write `easyci.yml`.** At minimum: `backend`, `report`, and one profile per event you care about
   (`pr`, `push`, release). Set `timeout_minutes` and `stall_minutes` per job from the project's real
   timings; quiet build steps (package installs, compilation) need a longer `stall_minutes`.
5. **Services:** a WSL job can get its own PostgreSQL with `services: {postgres: {}}`. For other
   services, or container images, use the `podman:<distro>` backend (`container_backend:`).
6. **First run with `--no-report`**, then read `easyci explain <run>`. Compare its verdict with the
   project's hosted CI on the same commit before letting EasyCI post statuses.
7. **Turn reporting on** (`report: true`), and in GitHub's branch protection decide whether the
   `easyci` context is required. Hosted jobs that EasyCI replaces can then be skipped there, the way
   JuliaFlow's `execution-policy` job does it.
8. **Optional:** `--desktop` launchers, `easyci dashboard` (for a config kept outside the project,
   `easyci register --config PATH --profile NAME --desktop` first), `easyci watch --once` in Task
   Scheduler for your own PRs, `pr_comment: true`, and `machine_label: true` if a second PC reports
   on the same PRs ([team.md](team.md)).

## Done when

- Three PR runs in a row finish unattended with the same verdict as the project's own CI.
- `easyci verify <run>` passes on them.
- The person who will use it day to day has started a run and read a result without a terminal
  (Desktop launcher or dashboard).

## Ithax, the second adopter

Ithax (Gravelaw/Ithax) runs from [`examples/ithax/easyci.yml`](../../examples/ithax/easyci.yml). What
onboarding it took, as a pattern for the next project:

- **Plain workflow steps, run natively.** Ithax has no runner of its own; `easyci plan` showed what
  EasyCI could not run yet. That was `astral-sh/setup-uv`, which EasyCI now checks like the other
  setup actions, plus two hosted-only jobs that the config skips (`all-green`, which EasyCI's
  own `easyci` context replaces, and `pr-title`, which needs the pull request's title).
- **A distro of its own,** `ithax-ci-ubuntu24`, with Python 3.11 and 3.13, uv, Node 22, Rust and
  Tauri's Linux packages on the default PATH, and Windows' folders left off it
  (`appendWindowsPath=false`), so a Windows `python` or `npm` is never picked up by mistake.
- **A matrix of Python versions on one distro.** One `python3` cannot be both 3.11 and 3.13, so the
  config sets `toolchains: {python: ignore}` and a `setup:` hook links the matrix's version
  (from `env: {EASYCI_PYTHON: "${{ matrix.python-version }}"}`) as `python` on `$GITHUB_PATH`,
  which is what `setup-python` does on GitHub.
- **Check against GitHub first.** The first run (`--profile quick --no-report`) matched GitHub's
  result on the same commit before reporting was turned on.

Things to find out for the next one stay the same: its stack and test command, its own runner if it
has one, services, GPU or large data, how long its jobs take and how much memory they need (`free -m`
inside the distro), and whether it needs anything in [../status.md](../status.md)'s limitations.
