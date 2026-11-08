# Getting started with EasyCI

This guide is for anyone who has a project on GitHub and wants its tests to run on their own
Windows PC, with the result shown on GitHub, without learning "DevOps" first. It takes about ten
minutes. If a word is new, the [CI/CD explainer](ci-explained.md) explains it.

## 1. Install the four tools (once per PC)

| Tool | Why | How |
| --- | --- | --- |
| **Python 3.11 or newer** | EasyCI is written in Python | [python.org/downloads](https://www.python.org/downloads/); tick "Add python.exe to PATH" |
| **Git for Windows** | gets an exact copy of your code | [git-scm.com](https://git-scm.com/download/win) |
| **GitHub CLI** (`gh`) | puts the green tick or red cross on GitHub | [cli.github.com](https://cli.github.com), then run `gh auth login` once |
| **WSL** (optional) | a real Linux inside Windows, where most projects' tests run best | in PowerShell as administrator: `wsl --install`, then restart |

Then install EasyCI itself. Open **PowerShell** and run, with the folder where you keep EasyCI:

```powershell
python -m pip install --user -e D:\EasyCI
```

Check that the PC is ready:

```powershell
python -m easyci doctor
```

Every line ends with either "ok" or a sentence that starts with **Fix:** and says exactly what to
do. Do what it says and run `doctor` again until all lines are fine.

## 2. Get your project ready (once per project)

Go to your project folder and let EasyCI look at it:

```powershell
cd D:\MyProject
python -m easyci setup
```

EasyCI tells you, in plain sentences, what kind of project it found (Python, Node.js, Julia, Go,
Rust, Java or .NET), where the tests will run (inside WSL or on Windows itself), and which files it
would add:

- a **workflow** file, `.github/workflows/ci.yml`, that lists the test steps, but only if your
  project does not have one yet. It is ordinary GitHub Actions, so GitHub can use it too;
- a small **`easyci.yml`** with EasyCI's own settings.

It asks before writing anything and never overwrites a file. To write, commit just those files and
put two buttons on your Desktop in one go:

```powershell
python -m easyci setup --commit --desktop
```

If something is missing (for example Node.js inside WSL), `setup` says so and how to install it.

## 3. Run the checks

Pick whichever you like:

- **Double-click** "Run checks - MyProject" on the Desktop. A window opens, runs every check and
  ends with one sentence such as *All checks passed in 4 minutes. GitHub shows green on commit
  abc1234.* Press a key to close it.
- **The dashboard:** double-click "EasyCI dashboard". Your browser opens a page on this PC with
  a **Run checks** button for each project, what is running right now, the live log, and, if
  something failed, why it failed in plain words.
- **A terminal:** `python -m easyci run --wait` in the project folder.

The checks keep running even if you close the window: they live in the background until they
finish. `python -m easyci status` tells you how far they got.

## 4. Read the result

- On **GitHub**, your commit or pull request shows `easyci` (overall) and one `easyci/<job>`
  check per job, green or red.
- `python -m easyci explain` (or the dashboard) says which step failed, shows the last lines of its
  output, and suggests what to try.
- Everything a run produced is kept under `%USERPROFILE%\EasyCI`, sealed with checksums;
  `python -m easyci verify` proves nothing was changed afterwards.

## 5. Optional extras

- **Run on every new pull-request commit:** `python -m easyci watch` checks your open pull requests
  every five minutes and runs the new commits. Only your own pull requests, never forks.
- **A comment on the pull request** with the result and the reason for a failure: add
  `pr_comment: true` to `easyci.yml`.
- **Let an AI agent use it:** `claude mcp add easyci -- python -m easyci mcp` gives the agent
  tools to start runs, wait for them and read why they failed.
- **Links from GitHub to the dashboard:** add `dashboard_url: http://127.0.0.1:8765` to
  `%USERPROFILE%\EasyCI\config.yml`. The links work on this PC only.

## When something goes wrong

| You see | It means | Do this |
| --- | --- | --- |
| "Waiting for the previous run to finish" | one run at a time, so your PC is not overloaded | nothing; it starts by itself |
| "Still running but nothing printed for 20 minutes" | a quiet step, common for Julia or big installs | usually wait; EasyCI stops it at the time limit |
| "EasyCI could not run JOB because the workflow uses ACTION" | that step needs something EasyCI does not do yet | nothing was run for that job; see `docs/usage.md` for what is supported |
| "The run was interrupted" | the PC restarted or went to sleep | run again |
| A red `easyci/...` check on GitHub | a test failed | `python -m easyci explain` |
