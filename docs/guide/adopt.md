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
8. **Optional:** `--desktop` launchers, `easyci dashboard`, `easyci watch --once` in Task
   Scheduler for your own PRs, `pr_comment: true`, and `machine_label: true` if a second PC reports
   on the same PRs ([team.md](team.md)).

## Done when

- Three PR runs in a row finish unattended with the same verdict as the project's own CI.
- `easyci verify <run>` passes on them.
- The person who will use it day to day has started a run and read a result without a terminal
  (Desktop launcher or dashboard).

## Notes for the next adopter (ithax)

Nothing about ithax has been inspected yet. When it is onboarded, find out:

- its stack and test command (`easyci setup` will detect the common ones);
- whether it has its own CI runner script to delegate to, or plain workflow steps to run natively;
- whether its tests need Linux, a database or other services, a GPU, or large data (`data:`,
  `artifact_threshold_mb`);
- how long its jobs take and how much memory they need. JuliaFlow's Julia tests needed a heap-size
  hint and extra swap on a 4 GiB WSL VM, so check the VM size (`free -m` inside the distro) early;
- whether it needs anything EasyCI lists as a limitation in [../status.md](../status.md), such as
  fork PRs or secrets from GitHub.
