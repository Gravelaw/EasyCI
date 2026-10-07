# Cloud development preflight

The EasyCI cloud checkout supports bounded repository development. It is separate
from the local Jenkins controller on TSLV-AJ and its Windows/WSL JuliaFlow agent.
These instructions do not start services, enroll agents, copy credentials, launch
jobs, expose hosted services, merge main or deploy a product.

## Establish source identity first

Run from the EasyCI checkout, not its parent directory:

```bash
pwd
git status --short --branch
git rev-parse HEAD
git rev-parse HEAD^{tree}
git branch -avv
```

Before editing, inspect applicable parent/repository `AGENTS.md` instructions and
relevant skills. Preserve existing changes. Record the actual branch and immutable
HEAD; environment configuration is not proof of either. Confirm the expected
source commit before creating a clearly named development branch. Do not merge
main as part of setup. Before any authorized push, verify the intended remote
branch, complete applicable checks and obtain independent review of the final
diff. Report failed or blocked required checks before pushing; do not treat them
as passes or bypass them. Never force-push over existing work.

On 2026-10-07 the initial cloud checkout was clean on local branch `work`, despite
the configured branch being `codex/jenkins-scaffold`. Its verified identity was:

- EasyCI commit: `4f9f022910ab3fc97beb17b3b22cc5cb72d2993e`.
- Git tree: `a2d374930bc548cfba8c1c14d35071f2959167ac`.
- Repository: `Gravelaw/EasyCI`.

These identify the assessed scaffold baseline, not every future checkout. The
documentation increment uses development branch `codex/cloud-preflight` from
that baseline. A read-only remote check also found `codex/jenkins-scaffold` at
the baseline commit before editing; that observation is not a permanent remote
state guarantee.

## Bounded checks

The runtime adapter requires Python 3.11+ and Git. Static validation and the
authorization tests additionally require PyYAML and jsonschema. The existing
suite uses Python's unittest; pytest is not required. Docker CLI with Compose
is needed only for the configuration-rendering checks below, not a running
engine. Bash provides shell syntax checks.

```bash
python --version
git --version
python -c "import importlib.metadata as m; print({n: m.version(n) for n in ['PyYAML', 'jsonschema']})"
python scripts/check-static.py
python -m unittest discover -s tests -v
docker compose config --quiet
docker compose -f compose.yaml -f compose.bootstrap.yaml config --quiet
bash -n agent/start.sh
bash -n controller/install-plugins.sh
git diff --check
git status --short --branch
```

Check each command's exit status; a later success does not clear an earlier
failure. Tests create and clean up their own temporary Git repositories and demo
releases. They do not connect to Jenkins. Compose commands above render local
configuration without starting containers. Do not replace them with `up`,
`build`, `run`, engine repairs or controller probes during cloud preflight.

For this task, official reputable task-local dependency setup is authorized if
needed. Prefer an isolated environment; record versions and source. This does
not authorize persistent credentials, trust/security changes or installation
from an unrecognized source. No dependency installation was needed for the
baseline checks below. A missing optional tool is a reported limitation, never
evidence that its check passed.

## Verified cloud baseline, 2026-10-07

| Observation | Result at the baseline commit above |
| --- | --- |
| Host | Linux x86_64 |
| Python / Git | 3.12.14 / 2.52.0 |
| PyYAML / jsonschema | 6.0.3 / 4.26.0 |
| Docker CLI / Compose | 28.4.0 / v2.40.3; daemon not queried |
| Java / Node / npm | OpenJDK 21.0.12.1 / v24.19.0 / 11.9.0; not JuliaFlow qualification |
| Static checker | Passed schema, policy, generated pipelines, 37 plugin pins and image digest consistency |
| Unit tests | 16 passed; JuliaFlow evidence/delivery uses a labeled stub |
| Final and bootstrap Compose rendering | Both passed |
| Shell syntax / Git whitespace | Passed |
| Baseline source checksum inventory | All 42 entries matched |

`evidence/files.sha256.json` is preserved as the historical baseline inventory.
It does not inventory this documentation increment: README.md and docs/setup.md
have changed, and this page is new. Verify that inventory against the baseline
commit, not an edited checkout. Use the new Git commit/diff for this change's
identity; do not rewrite historical evidence to imply it tested new content.

No applicable `AGENTS.md` or repository `SKILL.md` was found at initial assessment;
executor skill discovery returned no skills. Recheck when the checkout changes.
No missing JuliaFlow skill was established as a prerequisite for EasyCI-only
documentation, Python tests, schema validation or pipeline-generation work.
Historical evidence records a Julia engineering router selecting
`julia-manage-project-workflow`; that runtime is absent here. Before cross-repository
JuliaFlow work, inspect its actual checkout/guidance and obtain any required
approved skill package through the existing trust process. Historical skill
verification is not proof of present availability or permission to alter trust.

## Product and local-controller boundary

The task supplied JuliaFlow candidate
`4d71f1ec18cc3ab7551723fd7ba21f77fc26da31` and baseline
`11856e4d85f6c8ee96446687b4b21057733d9750`. They are product identities, not
EasyCI identities. Neither object is in this EasyCI checkout; the JuliaFlow
checkout and full BRD were not supplied here. Verify them in the product
repository before ingestion. Use explicit approved SHAs in the
[local snapshot procedure](setup.md#trusted-snapshot-ingestion).

The adapter requires native Windows and delegates to the existing WSL runner
with `--profile release-candidate --cache cold`. Cloud unit fixtures and the
historical successful Linux demo do not qualify JuliaFlow. Real qualification
still requires:

- The exact immutable candidate/baseline snapshot and complete cold release
  checks, with head/tree/base, phase evidence, checksums and cleanup verified.
- Full BRD coverage, independent Data Analyst and Data Scientist acceptance,
  and user acceptance referencing the same exact package digest.
- Security/plugin review, user-approved promotion of the exact tested bytes,
  retained receipts and a demonstrated rollback rehearsal.
- Separate real evidence for native Windows, offline installation and actual
  GPU behavior; Linux checks cannot certify these.

This is a requirements boundary, not a claim that the current scaffold enforces
every acceptance gate. Its pipeline has one local-admin review input; the
unavailable product delivery implementation needs separate inspection.

Local records describe a successful demo and 94 controller plugins, including
57 extras beyond the locked baseline. They also identify pending operational
qualification. These are historical local observations, not a fresh inspection
of TSLV-AJ. Preserve them and obtain approved exported evidence for later review;
do not connect to or modify that controller as part of cloud development.
