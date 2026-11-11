# Using EasyCI as a team, or on more than one PC

EasyCI is built for one person and one PC first. This page covers the next step: a second PC, or
a few people who each run EasyCI on their own machine and report to the same pull requests.

## What is shared and what is not

| Lives in the repository (shared) | Lives on each PC (not shared) |
| --- | --- |
| `.github/workflows/*.yml`: the checks themselves | `%USERPROFILE%\EasyCI\config.yml`: this PC's defaults |
| `easyci.yml`: profiles, timeouts, stall limits, `machine_label`, `pr_comment` | run evidence, caches, the artifact store |
| | the `gh` login, WSL distros, installed toolchains |

Rules of thumb:

- Put everything that describes **the project** in `easyci.yml` and commit it: which jobs a
  profile runs, how long a job may take, which jobs are skipped locally.
- Put everything that describes **one PC** in that PC's `config.yml`: its default backend
  (`backend: wsl:my-distro`), `machine_name`, `dashboard_url`, `cache_limit_gb`, `retention`.
- Never commit secrets. Pass host variables with `required_env`; EasyCI masks them in logs.

## Two machines on one pull request

Turn on machine labels in the shared `easyci.yml`:

```yaml
machine_label: true
```

Each PC then reports its own checks, `easyci/web@pc-a` and `easyci/web@pc-b`, plus `easyci@pc-a`
and `easyci@pc-b` overall, so the two never overwrite each other. Give each PC a short, stable
name in its `config.yml` (otherwise the Windows computer name is used):

```yaml
machine_name: lab-gpu
```

If branch protection should require a check, require the one from the machine that must pass
(for example `easyci@lab-gpu`). Without labels, both machines write to the same `easyci/...`
contexts and the last one to finish wins; that is fine for one PC and confusing for two.

## Splitting work between machines

Use profiles in `easyci.yml`, one per kind of machine, and run the right one on each PC:

```yaml
profiles:
  pr-fast: {event: pull_request, jobs: [lint, unit]}
  pr-gpu:  {event: pull_request, jobs: [train-smoke]}
```

On the everyday laptop: `easyci watch --profile pr-fast`. On the GPU workstation:
`easyci watch --profile pr-gpu`. Each PC queues only its own work and only on its own pull
requests (or everyone's non-fork pull requests with `--all-authors`).

## What EasyCI does not do (by design)

- No central server, no shared queue, no scheduling across machines: each PC runs its own queue.
- No GitHub App and no Check Runs with annotations. The owner chose plain commit statuses through
  each person's own `gh` login, which need no app, no private key and no new secret.
- No runs for pull requests from forks, ever, automatically: their code would run on your PC.
