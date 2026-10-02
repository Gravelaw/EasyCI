# Qualification record

## Live Linux demo, 2026-10-02

Demo build #2 completed SUCCESS after fixing isolated data-volume access.
All four stages ran: reviewed snapshot preparation, build/validation, explicit
local-admin review and promotion of the same tested artifact bytes. Read-only
checks confirmed source commit/tree identity, metadata/bundle checksums, result
checksum, matching built/promoted artifact checksums and reviewer receipt. The
Jenkins archive matches retained owner/result/promotion workspace evidence;
the promotion lock and staging directory were removed. See evidence/live-demo.json.
Demo uses a promotion.json destination record and release receipt; it has no
global current-release pointer. No additional build was triggered by verification.

Controller HTTP is healthy and anonymous management/API requests are denied.
Persisted Matrix Authorization grants only USER local-admin administration;
executors are zero, CSRF remains configured, signup is disabled and agent TCP is
disabled. The user configured the exclusive one-executor demo node and secret
privately. The agent runs as jenkins with all capabilities dropped, no privilege,
no published port, no socket or broad host mount, 2 GiB/2 CPUs/512 PID limits.
Only the approved read-only secret file is bound; data uses separate Docker
volumes. The official agent base also declares an anonymous remoting volume at
/home/jenkins/.jenkins. Its named-volume/retention refinement is still pending.
Secret contents, hashes and length were not accessed by the assistant.

The user-installed 57 extra plugins remain alongside the 37 locked baseline
plugins. Their full compatibility/security review and a newly locked image are
pending. The data-directory Dockerfile fix has not been rebuilt; the existing
runtime volumes were repaired and verified separately. Queue abort/restart,
backup/restore and full retention are not qualified by this successful demo.
JuliaFlow native Windows/WSL/GPU enrollment and its real 20-phase run remain
separate approvals and qualification work. Historical records below retain
their original observation scope and are superseded where this record applies.

## Approved activation, 2026-10-02

Both images built successfully. The first controller build exposed root ownership
of its temporary plugin manifest; `COPY --chown=jenkins:jenkins` fixes cleanup
without changing the runtime user. Sixteen host tests and static checks passed
after the fix. Bounded containers with network disabled independently verified
all 37 plugin hashes and recorded the agent tool/package inventory and derived
image identities in `evidence/image-build.json`.

The controller started using the bootstrap override. Read-only inspection verified
the sole port binding is 127.0.0.1:8080, named home storage, 2 GiB memory, 1.5 CPUs,
512 PIDs, all capabilities dropped, no-new-privileges and restart=no. HTTP returned
200, Jenkins 2.580.1 and the unlock wizard. No logs or unlock secret were read.
The user must complete the wizard and create `local-admin` before applying the
final matrix configuration. No agents were enrolled or jobs run. Plugin loading,
final JCasC/authentication behavior, pipeline execution and full JuliaFlow adapter
qualification remain pending. The records below retain the earlier state for
provenance and are superseded where this activation supplies new evidence.

Status: **reviewable scaffold with bounded host checks and actual Compose rendering;
live Jenkins integration not qualified**. The 2026-10-02 follow-up supersedes the
initial PATH-only Docker diagnosis and authorization configuration below. Work is a delegated local infrastructure prototype, not a new
tracked product-completion claim. No remote tracker, repository, review or
publication state has been changed.

Passed in this task-owned Windows workspace:

- PyYAML parses Compose/JCasC; checks enforce loopback-only ports, zero controller
  executors, explicit local-admin-only matrix permissions, manual security realm, named volumes,
  resource declarations, no privilege/socket/host mount default, and restart=no.
- Draft 2020-12 schema and both project contracts validate. Generated pipelines
  match contracts. Plugin/version/download-checksum manifests and image digests
  match Dockerfiles; shell inputs explicitly retain LF on Windows.
- Sixteen Python tests cover real temporary Git repositories, commit-pinned
  bundles and rejection of unauthorized group/user administrator grants. The original twelve adapter tests use
  bundles. The demo builds, tests and promotes identical serialized artifact
  bytes while preserving dirty source. Tampered bundle, metadata, result,
  artifact, source and evidence paths/hashes are rejected. Reviewer and busy
  promotion locks, overwrite rejection, and Windows-only platform checks pass.
- JuliaFlow's fixed Node runner invocation is checked. A clearly marked stub
  exercises evidence checking, original CD command delegation and durable copy.
  This does **not** execute WSL, the 20-phase suite or the real product package.
- Bash syntax and Git whitespace checks run with the existing Git tools.

Skipped / pending:

- Controller/agent image builds, plugin download/load compatibility, actual JCasC
  schema/wizard handoff and Jenkins pipeline parsing/execution. Both final and
  bootstrap `docker compose config --quiet` commands passed with installed Compose 5.5.1.
- Agent enrollment, file-secret consumption, auth/CSRF runtime checks, queue
  serialization, limits inspection, abort/restart/retention and backup/restore.
- Full JuliaFlow adapter run and promotion through Jenkins; Windows-native,
  Docker product and GPU certification. Existing candidate 099bb56 results from
  the delegation are prior-run evidence and are not attributed to this adapter.

Observed environment: Windows; Python 3.14.6, Node v26.3.0, Git available. Docker,
Podman and Java are absent from PATH. Extended read-only inspection discovered
Docker Desktop 4.92.0 in its per-user installation directory, with valid signatures,
CLI 29.8.0 and Compose 5.5.1. The Linux engine pipe is absent, no Desktop/backend
process runs, and ubn, juliaflow-ci-ubuntu24 and docker-desktop are all stopped.
WSL 2.7.14.0 is installed. No distro was started/stopped, and no software installation
or persistent service startup has occurred. See the dated follow-up evidence.

The signed Julia engineering runtime passed exact catalog integrity verification
against the external trust store. The router selected project-workflow guidance
under the task's already-authorized isolated mutation scope. Catalog and compact
route binding are retained in evidence/verification.json; no raw routing query,
trust key material or personal machine path is stored there.

Dependencies were resolved from official Jenkins metadata and Docker registry
manifests using approved read-only metadata requests. Anonymous registry pull
authorization was short-lived and never persisted. The downloaded metadata
identified 37 required plugin dependencies (including Matrix Authorization)
with core baselines <= 2.580.1.
This is a metadata compatibility check, not plugin execution. The demo image's
added apt tools are a remaining reproducibility gap until its inventory and
derived image digest have been reviewed and pinned.

Smallest first approval: launch already installed Docker Desktop 4.92.0 and inspect
engine health; user handles any terms/authentication prompts. No new installer,
WSL interruption or reset is indicated. Later approvals: build/download images/plugins;
start loopback controller (including Jenkins' initial unlock credential); manually
create approved admin; enroll each agent with its own private secret; explicitly
approve and coordinate use of the hardcoded JuliaFlow WSL distro/root runner.
The narrow Desktop launch is now approved but waits for the release-gate owner's
safe-window confirmation. Repository placement/publication is independently
approved for private Gravelaw/EasyCI on codex/jenkins-scaffold, superseding the
earlier undecided placement. Do not merge or deploy; verify remote state after
publication. Other activation approvals remain pending.
