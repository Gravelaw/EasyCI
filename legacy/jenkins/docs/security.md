# Trust and access

The default publishes only loopback HTTP 8080. Native agents may connect from the
same machine; other hosts require a separate network/TLS/authentication review.
No firewall, Docker daemon access, privileged mode, host PID namespace, broad
host mount, device passthrough or anonymous administrator is enabled. Runtime
containers drop all capabilities and disallow privilege escalation. No Jenkins
API token or hosted integration is required.

The final baseline uses checksum-pinned Matrix Authorization with a **user-typed
entry for local-admin only**, granting Overall/Administer. Anonymous users and
ordinary authenticated users receive no permissions. There is no authenticated
group, userOrGroup ambiguity, wildcard or fallback grant. This configuration
does not create the account. Multi-user operation needs separately reviewed
minimal role permissions. Jenkins administrators may override input submission
rules; they are trusted infrastructure operators, not an adversarial boundary.

The temporary compose.bootstrap.yaml override uses the standard Jenkins first-run
wizard rather than applying final matrix permissions before local-admin exists.
Only the user enters and submits unlock/password/account fields. Do not enroll
agents, install jobs or create another account during bootstrap. Restart with
compose.yaml alone immediately after the wizard to activate final authorization;
verify ordinary/anonymous users lack permissions before any agent/job enrollment.
The final matrix is the default configuration; the bootstrap override is opt-in.
Its behavior still requires live wizard/JCasC qualification. Official entry syntax
is documented by the [Matrix Authorization plugin](https://plugins.jenkins.io/matrix-auth/)
and its [JCasC test fixture](https://github.com/jenkinsci/matrix-auth-plugin/blob/main/src/test/resources/org/jenkinsci/plugins/matrixauth/integrations/casc/configuration-as-code-v3.yml).

Review infra changes before installing pipeline scripts or loading JCasC. Enable
Groovy sandbox; do not approve arbitrary signatures or globally trusted libraries.
No pipeline fetches its executable commands from unchecked job parameters. IDs
and hashes are validated before shell interpolation; commit/baseline are full
Git SHA values; adapters use fixed subprocess arguments. Both source and artifact
identities are checked before promotion, and promotion refuses overwrite.

Snapshots are administrative ingestion, not Jenkins SCM file checkout. Operators
must review source before providing its metadata hash. Source is cloned from a
verified bundle into a new job-owned checkout; tracked symlinks and submodules
are rejected. Symlinks/junctions and path escape in evidence are rejected. Full
baseline history is retained for existing control-document checks. There is no
Git plugin local-checkout protection bypass. Local filesystem Git ingestion is
performed only by the trusted operator and administrative adapter.

Trusted source can execute arbitrary code with agent permissions. The agent is
not a malicious-code sandbox and its enrollment secret is available to the agent
process/container. Do not run fork/unknown PRs or grant publication credentials
to build agents. Separate each project's node, snapshots, cache, artifacts and
release storage. The controller owns scheduling/authentication only.

Checksum manifests establish identity/integrity, not authorship. Jenkins input
captures the approved administrator identity and exact result checksum, then
the promotion adapter rechecks candidate/evidence/artifact bytes. JuliaFlow's
existing delivery command still enforces every original release gate. This
does not replace product review, GitHub check requirements, or authorization for
external release. No remote push, tag, release or deploy operation exists here.

Secrets remain outside source and Docker build contexts. Restrict secret-file
ACLs to the approved operator/agent. Do not record initial unlock passwords,
node secrets, tokens or environment dumps in evidence. Jenkins homes/backups
contain credential encryption keys and must be treated as sensitive even if no
API token has been added. Rotate/revoke each enrollment deliberately; do not
delete another controller's resources.

Dependency-lock.json records official registry image index digests and published
plugin download SHA-256 values. Resolve-lock.py reads anonymous public metadata
only; no persistent token is saved. Plugin downloads are verified inside image
builds. Review official advisories and the resolved OS layer before deployment;
being pinned does not certify absence of vulnerabilities. Derived image digests
and actual plugin compatibility remain live qualification evidence.
