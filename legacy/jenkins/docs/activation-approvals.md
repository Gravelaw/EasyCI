# Activation decision record - 2026-10-02

Already authorized: isolated scaffold development/review, tests, read-only host
diagnosis and source-contract inspection. The user subsequently approved the
narrow Desktop launch described below and creation/publication of private
Gravelaw/EasyCI. No persistent service or credential has been created. No merge
or deployment is authorized.

## Smallest first approval

**Launch the existing signed Docker Desktop 4.92.0 installation and inspect Linux
engine health only.** Exact app: `%LOCALAPPDATA%/Programs/DockerDesktop/Docker Desktop.exe`.
**Wait for the active full JuliaFlow release gate owner to confirm a safe window
before any engine launch/restart.** Read-only observations of stopped distros were
taken earlier and are not a guarantee that those resources remain idle. No WSL
shutdown/termination is authorized, even if installation/startup suggests it.
The operator/user may open this app from Start; an agent launch requires explicit
approval and must use a hidden window unless visible interaction is requested.
The user handles any license acceptance, sign-in, passwords and submissions.
Stop and report any upgrade/repair/reset/reboot/WSL-update or security/distro
integration prompt. This first approval does not include any of those actions,
Jenkins image builds, controller startup or enrollment. Leave autostart settings
unchanged. After launch, read-only `docker version`/`docker info` queries may
establish engine health; do not create a container as part of this first step.

Read-only findings:

- HKCU uninstall registration and installer logs identify per-user 4.92.0; logs
  show installation succeeded on 2026-09-25. Desktop, CLI and Compose executables
  have valid Docker Inc. signatures. CLI 29.8.0 / Compose 5.5.1 run successfully.
- No Desktop/backend process runs; the dockerDesktopLinuxEngine named pipe is
  absent. WSLService runs, vmcompute is stopped; all three WSL2 distros (ubn,
  juliaflow-ci-ubuntu24, docker-desktop) are stopped. Installed WSL is 2.7.14.0.
  No shutdown/termination/reboot is indicated by these findings. Startup health
  has not yet been tested; stop rather than disrupting a distro if repair asks.
- AutoStart=false was read from Docker settings; backend/integration/TCP exposure
  settings were not explicitly present in the inspected whitelist. Defaults were
  not inferred, settings were not changed, and no login/token files were read.
- No Podman installation was found in uninstall registrations, common paths or
  PATH. No alternative engine needs to be installed before trying existing Desktop.

The [official per-user installation instructions](https://docs.docker.com/desktop/setup/install/windows-install/)
match the discovered path. The current available official release is 4.93.0,
dated 2026-09-28 in the [release notes](https://docs.docker.com/desktop/release-notes/).
That is a fallback installer/version reference, **not an approved upgrade or a
reason to reinstall**. Existing 4.92.0 is the smallest restoration candidate.
If it fails, diagnose its exact failure read-only before proposing a repair.

The [Docker Desktop licensing page](https://docs.docker.com/subscription-billing/desktop-license/)
lists eligible personal/educational/noncommercial open-source use and small
business use (under both stated employee/revenue thresholds) as free; larger
commercial/government use requires a paid subscription. The user must confirm
the applicable category and handle agreement acceptance. No purchase, account,
token or automatic `--accept-license` operation is proposed.

## Later independent approvals

| Stage | Exact action / access | User handoff or constraint |
| --- | --- | --- |
| Image build/download | `docker compose build controller demo-agent`: two official digest-pinned images, 37 checksum-pinned plugins, demo-image Python/Git apt tools | Review apt inventory and resulting derived image digests; no host software installation |
| First-run controller | `docker compose -f compose.yaml -f compose.bootstrap.yaml up -d controller` | Persistent named home volume, Compose bridge, loopback HTTP 127.0.0.1:8080 only; Jenkins generates its initial unlock credential |
| Admin setup/final policy | User creates local-admin; then approved `docker compose up -d controller` without bootstrap override | User alone enters/submits credentials. Final matrix grants only this explicit user permission; verify before adding jobs/agents |
| Demo enrollment | Create one demo-linux node/executor and its enrollment secret; `docker compose --profile demo up -d demo-agent` | User handles secret storage/entry. Dedicated project volumes; WebSocket internal controller endpoint; no host agent port |
| Demo snapshots/job | Bounded no-network snapshot helper + reviewed UI pipeline | Trusted commit-pinned snapshots only; test build/review/promotion and abort/restart behavior before native enrollment |
| Native JuliaFlow, deferred | Java 21/agent.jar availability, approved local Windows account/node/secret and C:/JenkinsLocal paths | Only after container/demo qualification; coordinate root runner in hardcoded juliaflow-ci-ubuntu24 and its PostgreSQL/3 GiB swap resources |

No firewall changes, host/LAN exposure, Docker TCP API exposure, port 50000, host
mounts, socket grants, privileged containers or security bypasses are part of
this plan. No API token, OAuth grant, service account, Windows service, scheduled
task, GitHub integration or hosted CI is needed. Changes to Docker defaults or
security settings need a separate concrete review/approval. Images/Jenkins have
restart=no; no automatic startup is introduced by the scaffold.

## Repository and JuliaFlow source decision

The user selected and approved the dedicated private shared repository
**Gravelaw/EasyCI**, superseding the earlier local-ci-infra name proposal. Keep
reusable Jenkins operations there, with project-owned adapters referencing a
reviewed, pinned infra release. Publish the reviewed scaffold only to
codex/jenkins-scaffold; preserve any preexisting repository/content. Do not merge
or deploy. Current remote state belongs in a separately verified publication receipt.

**The current cold-cache native-Windows/WSL adapter requires no further JuliaFlow
source change.** It calls the existing committed `scripts/ci/wsl.mjs` and
`scripts/ci/delivery.mjs`, preserving the 20-phase validation and same-package
checksums. Source/workflow files in the product checkouts remain untouched.
A separately dedicated WSL distro would require a reviewed runner option because
the existing runner hardcodes juliaflow-ci-ubuntu24; project-isolated warm caches,
a Linux Docker orchestrator or GPU qualification are also separate follow-ups.
Do not silently patch those into the product to claim broader qualification.
