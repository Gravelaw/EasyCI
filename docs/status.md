# EasyCI status: spec vs delivered

The final state at the close of development (November 2026), measured against
[spec/PRODUCT.md](spec/PRODUCT.md) sections 14 (MVP acceptance) and 16 (roadmap). Evidence names refer
to tests under `tests/easyci/` and to real runs on the owner's PC against JuliaFlow
(Gravelaw/LowcodeMLtoolkit.jl).

## MVP acceptance criteria (section 14)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Doctor | Done | `RealHost.test_doctor_on_this_host`; `test_cli_json.test_doctor` |
| 2 | Start returns fast | Done | Live runs return the run id at once |
| 3 | Survives the launcher | Done | `RealHost.test_run_survives_a_session_that_kills_its_job` (a session that kills its job object on close); a closed cmd window, tested live |
| 4 | Unattended JuliaFlow PR profile | Done | Exit runs below |
| 5 | Statuses on the commit | Done | Four `easyci` contexts on each exit-run commit, each with the run id |
| 6 | Orphan reaping | Done | `test_killed_supervisor_is_reaped`; `Reaper.*` |
| 7 | Queue | Done | `test_detached_supervisor_outlives_its_launcher`; `busy_paths` for a project's own runner |
| 8 | Stall | Done | `RealWsl.test_stall_kill_and_cancel` |
| 9 | Timeout | Done | `RealWsl.test_timeout_kills_the_process_group` |
| 10 | Cancel | Done | `RealWsl.test_stall_kill_and_cancel`; native cancel tests |
| 11 | Plan import | Done | `test_workflow.JuliaFlowImport` |
| 12 | Unsupported is honest | Done | `test_unsupported_and_toolchain_mismatch_run_nothing` |
| 13 | Toolchain verification | Done | `RealWsl.test_toolchain_verified_in_distro`; Julia depot probed |
| 14 | Evidence sealed | Done | `SealAndVerify` |
| 15 | JSON discipline | Done | `test_cli_json` |
| 16 | Plain status | Done | `Sentences`; metrics named in the sentence |
| 17 | Redaction | Done | redaction tests, including multi-line and split secrets |
| 18 | Tests under 60 s | Done | About 35 s for the whole suite (parallel worker processes) |

**MVP exit condition, three consecutive unattended PR runs on real commits: met.** The heads of
JuliaFlow PRs #65, #64 and #63 ran back to back without anyone watching, and each reported to GitHub.

- `5484bf6`: passed, 89 min.
- `ed7ec11`: failed, 81 min. This is JuliaFlow's own verdict, not an EasyCI error: the Dependabot
  bump changes `package.json` without the control-doc updates JuliaFlow's rule requires.
- `846a025`: passed, 80 min.

## Roadmap (section 16)

| Phase | Item | Status |
|---|---|---|
| 1 | Expressions (comparisons, functions, `hashFiles`, contexts) | Done |
| 1 | `strategy.matrix` with include/exclude, fail-fast, `needs` aggregation | Done |
| 1 | Local caches for `actions/cache` | Done |
| 1 | `download-artifact` between jobs | Done |
| 1 | PostgreSQL service and job `env` on WSL | Done (real-WSL test passes) |
| 1 | Jenkins scaffold moved to `legacy/jenkins/` | Done |
| 1 | `setup:` / `cleanup:` hooks (added at close-out) | Done |
| 1 | Retire JuliaFlow's `workflows.json` parity check | In progress via Gravelaw/LowcodeMLtoolkit.jl#67: EasyCI's native config is now JuliaFlow's default, and JuliaFlow removes its own runner there |
| 1 | **Exit: native `web`/`julia` match delegated on 5 commits** | Met: same verdicts on 5484bf6, ed7ec11, 846a025, 019f15c and 3f0da7d (details below) |
| 2 | `easyci setup` for Node, Python, Julia, Go, Rust, Java, .NET | Done |
| 2 | Desktop launchers, localhost dashboard | Done |
| 2 | `easyci watch`, PR comment, `target_url`, `easyci mcp` | Done |
| 2 | Exit: the owner runs and reads a result without a terminal | Ready, not yet tried by the owner |
| 3 | Container backend (`podman:<distro>`, services, job containers) | Done; Podman instead of Docker (owner's choice) |
| 3 | Artifact store, `data:` hashes, `compare`, GPU check | Done |
| 3 | `promote` with receipt and fail-closed lock | Done |
| 3 | **Exit: release candidate runs natively and promotes** | Met: rc-native passed in 56 min on 019f15c; promoted with a verified receipt (details below) |
| 4 | Machine labels in status contexts | Done (two simulated machines) |
| 4 | Team guide; Podman vs Dagger evaluation | Done |
| 4 | GitHub App for Check Runs | Not done: owner's decision |

## Known limitations

- **No fork PRs and no GitHub secrets.** EasyCI runs your own code on your own PC. `secrets`, `vars`,
  `inputs`, `github.actor` and PR numbers are unsupported in expressions rather than read as empty.
  `required_env` passes chosen host variables through, masked.
- **Windows host only.** Backends are `native` (Git Bash), `wsl:<distro>` and `podman:<distro>`.
  macOS and Linux hosts are not supported.
- **Process-based services only on `wsl`.** PostgreSQL is the only built-in service; anything else
  needs the Podman backend. Podman volumes, registry credentials and extra container options are
  reported unsupported.
- **Hosted-only actions.** Actions other than checkout, the setup actions for Node, Python, Julia, Java and Go, cache, and upload/download-artifact are
  not run. A job that needs one must replace it with `command:`, `skip_steps` or a `run:` step.
- **Memory on small WSL VMs.** The WSL VM is shared by every distro. Heavy jobs may need what the
  project's own runner gives them (JuliaFlow: a heap-size hint and temporary swap, through
  `setup:`/`cleanup:`).
- **Commit statuses, not Check Runs.** No annotations on files; the failure excerpt is in the status
  description, `easyci explain` and the optional PR comment.
- **One machine verified.** Two-machine reporting is tested with two simulated EasyCI homes, not two
  real PCs.
- **Python 3.11:** everything passes except the legacy Jenkins scaffold's jsonschema test, which is
  outside the default suite.
- **`actions/cache` inside job containers** is always a miss and is never saved. The cache store
  lives in the WSL distro, not in the image, so the install steps simply run.
- **Container and service settings** may only use `github` and `matrix` values. Values from `needs`,
  `env` or `steps` are refused with a reason, because containers start before any step.

## Exit checks in detail

**Phase 1, native vs delegated.** JuliaFlow's `web` and `julia` jobs ran from their own workflow
steps (then `examples/juliaflow/easyci.native.yml`, profile `pr-native`; now the default
`examples/juliaflow/easyci.yml`, profile `pr`) and through JuliaFlow's runner (now
`legacy/juliaflow/easyci.delegated.yml`). All of these were cold-cache runs with no GitHub reporting.

| Commit | `web` native / delegated | `julia` native / delegated |
|---|---|---|
| 5484bf6 | passed / passed | passed (15 min) / passed (32 min) |
| ed7ec11 | failed at the control-doc rule / same | passed (14 min) / passed |
| 846a025 | passed / passed | passed (21 min) / passed (28 min) |
| 019f15c | passed / passed | passed (22 min) / passed (29 min) |
| 3f0da7d | failed at the control-doc rule / same | passed / passed (35 min) |

The first round of native `julia` runs was killed for lack of memory. The native profile now gives
Julia the same heap-size hint and temporary swap as JuliaFlow's own runner, through the new
`setup:` and `cleanup:` keys.

**Phase 3, release candidate.** Run 20261111-194855-4d2e (`rc-native`, commit 019f15c) passed in
56 minutes, with its `cleanup` hook releasing the swap file. `easyci promote --artifact
debian-release-verification` copied 23 files and a `receipt.json` into a release folder, and
`easyci verify` on that folder reports no mismatches.

## JuliaFlow cache

JuliaFlow's default is now native (`examples/juliaflow/easyci.yml`). Its Julia depot, npm cache and
Playwright browsers stay in the distro between runs, which is the warm path; the `pr-cold`,
`push-cold` and `release-candidate-cold` profiles give each job empty ones through `setup:`.

Before the switch, the delegated configuration passed `--cache warm` to JuliaFlow's runner, which
keeps a locked, dependency-keyed cache in the distro. All the evidence above was gathered cold.

Timing of the delegated `julia` job on commit 019f15c:

| Cache | Run | `julia` job |
|---|---|---|
| cold | 20261108-185357-be24 | 29 min (cold runs on other commits: 28-35 min) |
| warm, first run (fills the cache) | 20261111-221035-1135 | 34 min |
| warm, second run | 20261111-221037-1bd1 | 13.6 min |

The second warm run reused the Julia depot under `/ci-cache/<key>`, so the job took less than half
the cold time. Native `julia` runs (14-22 min) keep their depot in the distro's home folder, which is
why they were already faster than cold delegated runs.

## Code review

Every phase was reviewed by Codex on GitHub, and each push was reviewed again. About 60 findings
were fixed, each with a test that fails without its fix, and every review thread is resolved.
