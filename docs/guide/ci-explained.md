# CI/CD explained, using JuliaFlow as the example

This guide is for someone who builds software but has never had to think about
"DevOps". It explains the ideas behind EasyCI in plain language, using the
JuliaFlow / LowcodeML project as the running example. No prior knowledge is assumed.

## 1. What DevOps is

Writing code is only half of shipping software. The other half is everything that
happens *around* the code: checking that a change didn't break anything, building
the thing users actually install, keeping a record of what was released, and being
able to roll back when something goes wrong.

**DevOps** is the name for that second half, and for the habit of automating it.
"Dev" is development (writing the code). "Ops" is operations (building, releasing
and running it). DevOps means the same people own both, and repetitive steps are
done by machines rather than by memory.

A kitchen analogy: cooking the dish is development. DevOps is the kitchen routine
— tasting before it leaves the pass, labelling and dating what goes in the fridge,
writing down the recipe version so the next batch tastes the same.

## 2. What CI/CD is, and why JuliaFlow needs it

**CI — Continuous Integration.** Every time you change the code, a machine
automatically runs the full set of checks and tells you "pass" or "fail". The point
is to catch breakage within minutes of causing it, while you still remember what
you changed.

**CD — Continuous Delivery.** When the checks pass on a version you want to
release, a machine builds the release package in a repeatable way, records exactly
what went into it, and stores it safely. "Continuous" just means it happens every
time, the same way, without hand steps.

JuliaFlow is a good example of why this matters. One change can touch three
different worlds at once:

| Part of JuliaFlow | What its checks do (from `.github/workflows/ci.yml`) |
| --- | --- |
| **Web front end** (TypeScript) | Install packages, validate the data contracts, build the web app, run the unit tests, check documentation and requirement traceability |
| **Julia back end** | Install the exact Julia packages, then run the Julia test suite (for example the 100 Gateway Repository and 221 Worker Service tests) against a real PostgreSQL database |
| **Release gate** | Build the full product, start it, and drive five real browser tests with Playwright against the live back end |

Nobody can run all of that by hand after every edit. It takes hours and has dozens
of steps. Without CI, a change to the web form could silently break a Julia
endpoint and you would find out weeks later. With CI, the PR shows a red cross the
same afternoon, with the log of the exact failing step.

For an AI coding agent the need is even stronger. Agents make many changes fast;
CI is the referee that tells both you and the agent whether a change is actually
good, instead of trusting the agent's own claim.

## 3. What GitHub Actions does, and why it costs money

**GitHub Actions** is GitHub's built-in CI/CD service. The files in
`.github/workflows/` are its instructions: "when someone opens a pull request, start
a fresh Linux computer, check out the code, install Node and Julia, run these
commands". GitHub rents you that fresh computer (a *runner*) for as long as the job
takes, then throws it away. The green ticks and red crosses on a pull request come
from these runs.

It costs money because GitHub is lending you real computers. For **private**
repositories (both JuliaFlow and EasyCI are private) you get a monthly free allowance
of runner minutes (about 2,000 on the free plan, 3,000 on Pro), then pay per
minute. Since January 2026 a standard Linux runner costs about **$0.006 per minute**
(Windows about $0.010, macOS about $0.062). Public repositories are free.

A full JuliaFlow PR check is web + Julia + release gate. The release gate alone is
allowed up to 180 minutes, and a cold Julia install can take most of an hour. So
one push can use **2–4 runner-hours**. Ten pushes a week uses up the free allowance
in roughly a week. The dollar amount after that is not huge, but if the account's
spending limit is $0, GitHub simply **stops starting jobs**. That is exactly what
happened on one JuliaFlow PR: the jobs never began, so there was no answer at all.

GitHub also lets you connect your *own* computer as a "self-hosted runner". Those
minutes are currently free. GitHub announced a small per-minute fee for them in
December 2025 and withdrew it after complaints, so the price could change again.

## 4. How the local approach differs

The local approach runs the same checks on **your own PC** instead of a rented
GitHub computer. On this machine that means a dedicated Linux environment inside
Windows (WSL, the `juliaflow-ci-ubuntu24` distro) with the right versions of Node,
Julia and PostgreSQL installed once.

| Question | GitHub-hosted runner | Local (EasyCI) |
| --- | --- | --- |
| **Cost** | Free quota, then per minute; blocked by a $0 limit | Free; uses your electricity and PC time |
| **Speed** | Fresh machine every time, so installs start cold; you wait in a queue | Can reuse warm caches; no queue, but your PC is busy while it runs |
| **Trust** | Neutral machine; reviewers trust the green tick | Your own machine; trust comes from the evidence it records and from honest reporting |
| **Evidence** | Logs kept by GitHub for a while | Full logs, versions, checksums and artifacts kept on your disk for as long as you want |
| **Availability** | Always on | Only when your PC is on and awake; a sleeping laptop runs nothing |
| **Fidelity** | The reference environment | Close, but not identical: a WSL pass does not prove Windows-native or Docker behaviour |

JuliaFlow already tried a do-it-yourself version of this (its `scripts/ci/` runner).
It works, but it taught three lessons that EasyCI is designed around:

1. **Runs must not die when the window closes.** Several JuliaFlow runs stopped
   when the terminal or agent session that started them ended, and were left
   showing "running" forever. EasyCI starts each run as its own background process.
2. **Stuck runs must be noticed.** One Julia package install sat idle for over
   seven minutes and needed a human to kill it. EasyCI watches for silence and
   marks a run as stalled.
3. **Results must show up where you look.** Local passes were invisible on GitHub,
   so a PR only showed "skipped". EasyCI posts the local result back to the PR as a
   status, using your existing GitHub login.

## 5. What Jenkins is and does

**Jenkins** is the oldest and most widely used self-hosted CI server. You install
it on your own machine; it gives you a web dashboard, a job queue, a list of
connected worker machines ("agents"), and more than a thousand plugins for almost
anything. A *Jenkinsfile* describes the steps of a job.

The earlier EasyCI work built a careful Jenkins setup: Jenkins running inside
Docker, 37 locked plugins, an admin account, and a demo job that passed. It
proved the idea, but it also showed why Jenkins is a poor fit here:

- It needs Docker (currently broken on this PC) and Java (not installed) before it
  can run anything.
- Every run needed a person to create a snapshot, copy a 64-character code into a
  form, and click approve — hard for a beginner, awkward for an AI agent.
- It does not read the existing `.github/workflows` files, so every check would be
  described twice.
- It is a large system to keep patched and safe for one person.

Jenkins is powerful, like a commercial kitchen. For one developer and their AI
agents, a well-organised home kitchen is a better fit. That is why the EasyCI spec
keeps the good ideas from the Jenkins work (exact-commit snapshots, checksums,
never-overwrite releases) but replaces Jenkins itself with a small tool.

## 6. How this serves data science and your projects

Data-science projects have needs that ordinary web CI handles badly. EasyCI aims
to cover them:

- **Long jobs.** Training or a full validation can take hours. Local runs have no
  per-minute bill and no hosted time limit, and the background supervisor keeps
  them alive.
- **GPUs.** GitHub's standard runners have no GPU. Your own machine can run the
  real CUDA path (JuliaFlow's hardware smoke test currently skips CUDA on hosted
  runners for exactly this reason).
- **Big files.** Datasets and trained models do not belong in Git. EasyCI keeps
  run artifacts in a local store and records their checksums, and works alongside
  tools such as DVC that version data separately.
- **Reproducibility.** Every run records the exact commit, the Julia and Node
  versions, the package lock files, and checksums of the outputs. Months later you
  can say exactly what produced a given model or release.
- **Comparing results.** A job can write a small metrics file (accuracy, run time,
  row counts). EasyCI shows it in the status so a change that makes a model worse
  is as visible as one that breaks a test.

For JuliaFlow concretely: you push a branch, EasyCI runs the web, Julia and
release checks on your PC, the PR shows "easyci/web ✓, easyci/julia ✓,
easyci/release ✗", and `easyci explain` tells you in plain words which step failed
and where the log is. When a release candidate passes, the exact tested package is
promoted into a numbered local release with a receipt — the same bytes that were
tested, never rebuilt.

## Glossary

| Term | Meaning |
| --- | --- |
| Workflow | A YAML file in `.github/workflows/` listing jobs and their steps |
| Job | A group of steps that runs on one machine (e.g. `web`, `julia`) |
| Runner / agent | The computer that actually executes a job |
| Pull request (PR) | A proposed change, shown on GitHub with its check results |
| Commit status | The ✓ / ✗ / ● next to a commit on GitHub |
| Artifact | A file a job produces and keeps, such as a log, report or package |
| Evidence | EasyCI's record of a run: logs, versions, checksums, outcome |
| WSL | Windows Subsystem for Linux: a real Linux environment inside Windows |
| Docker | A tool for running programs in isolated "containers" |
