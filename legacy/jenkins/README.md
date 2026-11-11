# Legacy: Jenkins scaffold (frozen)

This folder is the **frozen** Jenkins scaffold (EasyCI spec section 4). It was moved here from the
repository root in EasyCI Phase 1 and is kept for reference with its tests and evidence; it is not
maintained and nothing in EasyCI depends on it. Its snapshot logic lives on, re-implemented, in
`easyci/snapshot.py`. Run its checks from this folder:

```powershell
python scripts/check-static.py
python -m unittest discover -s tests -v
```

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
