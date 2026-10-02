# Retention, restart, backup and recovery

All maintenance commands require an approved operator session and exact resource
identity checks. These examples do not grant permission to delete data or start
services. Compose project is `jenkins-local`; volume names are explicit. Avoid
name collisions with an existing stack before first use.

Persistent storage:

| Storage | Contents | Retention |
| --- | --- | --- |
| jenkins-local-controller-home | jobs, auth realm, keys, build history, plugin state | durable; backup before changes |
| jenkins-local-demo-workspace | per-build runs/<number>, partial failure evidence | operator-reviewed cleanup only |
| jenkins-local-demo-cache | reserved demo-only cache | unused by current demo; no cross-project cache sharing |
| jenkins-local-demo-snapshots | approved immutable source bundles | keep until associated runs/releases are archived |
| jenkins-local-demo-releases | same-byte promoted demo artifacts and receipts | never overwritten or automatically deleted |
| C:/JenkinsLocal/.../juliaflow | native snapshots/workspaces/releases | separate project root, retained locally |

Jenkins keeps 20 builds / 30 days, with archived files for the latest 10 builds.
Container JSON logs rotate at 10 MiB x 3. This does **not** delete agent workspaces
or releases. No broad pruning job is installed. The JuliaFlow runner independently
retains its latest three completed runs within each isolated checkout; this
adapter normally creates one run per new checkout, so it does not purge another
Jenkins checkout. Failure/interrupted directories stay available for diagnosis.

To clean a run, first take its node offline and ensure no executor, runner process,
review input or promotion lock is active. Resolve the exact run path under that
project's configured `runs` root; verify owner.json, project, snapshot ID and build
number; reject any symlink/junction; verify archived evidence/receipt/checksum;
then remove **only that checked run directory** using the same native shell.
On Windows use `Remove-Item -LiteralPath <verified-absolute-path> -Recurse` after
confirming confinement. Never build deletion command strings, traverse into a
different shell, or use `docker system prune`, wildcard root deletes, WSL shutdown,
`down -v` or cache deletion as a generic cleanup fix. Releases require a separate
deliberate retention decision. A stale promotion lock is a failed/interrupted
operation, not permission to delete it automatically; inspect pending output first.

`restart: no` prevents automatic startup after engine/host reboot or a crash.
Use `docker compose stop` (or `--profile demo stop` for agent + controller) for
approved stops; `docker compose down` removes stack containers/network and keeps
named volumes. No `--volumes` flag. Native Windows agents stay foreground/manual.
Jobs have bounded outer and validation/review timeouts. Existing WSL runner owns
its subprocess cleanup and evidence; loss of an agent or forced engine kill can
leave interrupted resources. Review exact runner ownership before recovery.

Jenkins may resume pipeline state after restart. The adapter refuses existing
workspaces/results/releases and a held promotion lock; it never silently rebuilds
or overwrites. An interrupted promotion after upstream JuliaFlow CD but before
durable copy may leave a valid local source receipt and pending durable output.
Reconcile its bytes and review receipt manually, preserve evidence and start a
new reviewed build if needed. Do not blindly replay CD or unlock a busy job.

## Back up

Quiesce Jenkins, take nodes offline, wait for jobs and review waits to finish or
abort deliberately, and stop controller/agent. Record the exact Compose project,
image digests, dependency lock, volume inventory and native roots. Use a local
private backup destination; no cloud upload is implied.

For each confirmed named volume create a bounded, no-network helper from the
already built local demo-agent image, mount only that volume read-only, and
produce a tar under the helper's /tmp. Example for controller home:

```powershell
docker create --name jenkins-local-backup-home --network none --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges:true --memory 256m --cpus 0.5 --pids-limit 32 --mount type=volume,src=jenkins-local-controller-home,dst=/data,readonly --entrypoint bash local/jenkins-demo-agent:scaffold-1 -c "tar -cpf /tmp/controller-home.tar -C /data ."
docker start -a jenkins-local-backup-home
docker cp jenkins-local-backup-home:/tmp/controller-home.tar "<private-backup-directory>/controller-home.tar"
docker rm jenkins-local-backup-home
Get-FileHash -Algorithm SHA256 -LiteralPath "<private-backup-directory>/controller-home.tar"
```

Repeat for only the confirmed project volumes, including release/snapshot storage.
Verify helper exit status is zero before copying; retain checksums/inventory.
Preexisting ownership may require a reviewed operator adjustment; do not broaden
capabilities. Make native-root backups while its agent is offline using a trusted
archive tool; preserve ACLs/receipts and verify hashes. Encrypt sensitive backup
storage using an approved existing method. Keep at least two dated backups and
the pre-upgrade backup; automatic backup scheduling is not configured.

## Restore / upgrade

Test restore into **new** explicitly named volumes and separate native roots
while the production stack remains stopped. Validate backup checksums and archive
paths before extraction (no absolute paths, traversal, links or devices). Use a
no-network bounded helper to copy the archive in, mount only the new empty
volume, and extract as UID 1000. Do not overwrite active storage. Select those
new volumes with a separately reviewed Compose override, maintain loopback-only
binding, and start only after approval. Preserve the entire Jenkins home,
including `secrets/`, `secret.key` and encryption keys; selective restore can
make credentials unusable. Offline plugin/JCasC/image versions must match the
backup before assessing an upgrade.

Verify authentication, anonymous denial, zero controller executors, node identity,
project isolation, retained hashes, job history and a fresh demo build. Then
perform a separate real JuliaFlow adapter run; a restored Jenkins UI alone does
not establish product readiness. Never expose two cloned controllers with the
same enrollment secrets simultaneously. Re-enrollment/secret rotation needs its
own approval. Rollback is an approved restart with the preserved pre-upgrade
home/image/config versions; don't downgrade an already migrated home in place.

## Live qualification checklist

After approvals/tool restoration: render Compose with the actual engine; build
and record derived image digests/OS inventory; verify all plugin hashes/load
compatibility; complete wizard; verify auth/JCasC/controller limits; enroll demo;
test success, rejection, abort, restart and unavailable-agent paths; run two heavy
jobs to observe queue serialization; exercise retention and a stopped-volume
backup/restore. Then enroll/coordinate native Windows WSL execution, run complete
same-candidate cold JuliaFlow validation, review its 20 phases/evidence/cleanup,
and promote the exact staged package. GPU/native/Docker product qualifications
remain separate. Do not call this stack production-ready before these checks.
