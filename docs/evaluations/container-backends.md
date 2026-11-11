# Evaluation: Podman and Dagger as container backends (spec Phase 4)

Written 2026-11-08 for the EasyCI roadmap item "Dagger or Podman backend evaluation". Both were
judged against what EasyCI needs on one Windows PC: run GitHub `container:` and `services:` keys
faithfully, keep working when Docker Desktop is broken (it is on the reference PC), need no
admin GUI and no paid licence, and be drivable from Python without a long-running daemon of our own.

## Decision

**Podman inside a dedicated WSL distro is the container backend** (`podman:<distro>`, built in
Phase 3, verified on the reference PC with Podman 4.9.3). **Dagger is not adopted.**

## Podman (adopted)

- Runs rootful inside a WSL distro made only for it (`easyci-containers`), installed with one
  `apt-get install podman`. No Docker Desktop, no Windows service, no licence question.
- Speaks the same image and container model as Docker, so GitHub's `services:` (image, env, ports,
  health checks) and `container:` map almost one to one: a network per job, service containers with
  network aliases, a job container driven with `podman exec`.
- Daemonless: every call is a plain command, which fits EasyCI's model of a supervisor that drives
  processes and records their output and exit codes.
- Limits found: rootful in a dedicated distro rather than rootless (simpler on WSL, contained by
  the distro); image pulls on first use are slow; `volumes:`, `credentials:` and options other than
  health checks are reported unsupported rather than guessed.

## Dagger (not adopted)

- Dagger runs pipelines written with its own SDKs (Go, Python, TypeScript) on a container engine.
  It does not read GitHub Actions YAML, so using it would mean describing every check a second time,
  which is the duplication EasyCI exists to remove (spec section 1, root cause c).
- It still needs a container engine underneath (Docker, or Podman with extra setup), so it adds a
  layer on top of the Podman backend instead of replacing one.
- Its caching and DAG model are strong for build pipelines, but EasyCI's local caches
  (`actions/cache` on disk, warm Julia depots inside WSL) already cover the cases that matter here,
  and its engine runs as a long-lived container that EasyCI would have to supervise as well.
- Revisit if a project wants to keep its pipeline in code rather than YAML; EasyCI could then
  delegate one job to `dagger call ...` the way it delegates JuliaFlow's jobs to `wsl.mjs`, with no
  change to EasyCI itself.

## Not evaluated further

- **Docker Desktop**: broken and uninstalled on the reference PC; its licence and admin
  requirements also argue against making it a dependency. The Podman backend uses the same images.
- **nektos/act**: replays workflows in Docker; EasyCI already imports the workflows itself and needs
  no Docker.
