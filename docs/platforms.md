# Supported execution boundaries

| Scenario | Scaffold path | Evidence / qualification |
| --- | --- | --- |
| Linux container controller | Official Jenkins 2.580.1 Java 21, pinned index digest | Image built; localhost health, local-admin matrix policy, anonymous denial, zero executors and disabled agent TCP verified |
| Linux compatible project | Separate official inbound agent + Python/Git layer | Exclusive one-executor demo build #2 SUCCESS; snapshot, user review and identical-byte local promotion verified; other projects require their own qualification |
| JuliaFlow Node-to-WSL | Native Windows inbound agent, existing scripts/ci/wsl.mjs | Command contract and rejection checks pass; live adapter pending |
| JuliaFlow Linux container full CI | No adapter claimed | Existing Docker runner needs separate approved host orchestrator; no engine/socket in Linux Jenkins agent |
| Native Windows product tests | Separate native agent | No Windows product certification from WSL results |
| GPU jobs | Separate approved hardware agent and explicit label | No GPU runtime qualification or device pass-through provided |
| Podman rootless | Potential Compose alternative | Not installed/discovered or qualified here; secret/volume/resource/Compose behavior must be tested |
| Hosted GitHub workflows | Existing product policy | Untouched; skip policy remains separate work |

Container limits: controller 1.5 CPUs / 2 GiB RAM / 512 PIDs; demo agent 2 CPUs /
2 GiB RAM / 512 PIDs; snapshot helper 0.25 CPU / 128 MiB / 32 PIDs. JVM heaps are
1 GiB / 256 MiB respectively. Controller and running demo-agent limits were
verified by engine inspection; snapshot-helper limits remain configuration
declarations. See [demo evidence](../evidence/live-demo.json) and the
[qualification record](qualification.md). Named volumes have no portable
Compose disk quota. Plan at least
10 GiB for Jenkins/plugin/history growth and 5 GiB for demo storage; check free
space regularly. These are planning estimates, not measured bounds.

The current controller has 94 plugins: 37 checksum-pinned baseline plugins and
57 user-installed extras from the setup wizard. The extras are preserved; their
full compatibility/security review and a newly locked image remain pending.
The demo data-volume access repair was verified live. The Dockerfile fix for
future fresh volume initialization has not been rebuilt/deployed. The official
agent image's anonymous remoting volume remains a retention refinement.
No full backup/restore, abort/restart or cross-project queue qualification is
implied by the successful demo.

Native Windows/WSL jobs do not inherit container limits. Budget at least 3 GiB
free disk for each runner-owned swap plus source, dependencies, database and
artifacts; arrange a user-approved RAM/CPU/disk budget before enrollment. The
existing runner serial precompile/heap hints do not cap native allocations.
Never claim that a WSL heap hint or Jenkins executor count enforces RAM limits.

For another project: add a schema-valid contract, implement an allowlisted adapter
with typed arguments, create separate project-specific workspace/cache/snapshot/
release volumes or native roots, regenerate/review its pipeline, and qualify its
actual platform. Do not reuse demo storage or labels across projects. The schema
currently supports only the two implemented adapters; it accepts no arbitrary
shell command. PR/push JuliaFlow profiles validate only; their results are not
promotable and the current delivery pipeline will refuse promotion.
