"""`easyci watch`: queue a run for every new head of your open pull requests (spec Phase 2).

It polls `gh` (no webhook, no inbound port). Only pull requests from branches of the repository
itself are considered, never forks, and by default only the logged-in user's own pull requests,
because a run executes the pull request's code on this PC. A head that was already queued is not
queued again. `--once` checks a single time, for Task Scheduler; otherwise it checks every
`--interval` minutes until stopped.
"""
import argparse
import subprocess
import time

from easyci import github, snapshot, store


def state_path(slug):
    return store.home() / 'watch' / f'{slug}.json'


def ensure_commit(repo, sha, ref):
    """Make the pull request head available locally (fetch with the user's own git and credentials)."""
    if snapshot.resolve(repo, sha):
        return True
    for target in [t for t in (ref, sha) if t]:  # the branch, then the commit itself (GitHub allows that)
        try:
            subprocess.run(['git', 'fetch', '--no-tags', '--quiet', 'origin', target], cwd=repo, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if snapshot.resolve(repo, sha):
            return True
    return False


def cycle(repo, profile=None, config=None, all_authors=False, no_report=False, start=None):
    """One check. Returns [{'pr', 'sha', 'run_id' | 'skipped'}] for the pull requests that changed."""
    from easyci import cli
    remote = github.parse_remote(snapshot.origin_url(repo))
    if not remote:
        raise cli.Fail(2, 'NO_GITHUB_REMOTE', 'origin is not a GitHub repository, so there are no pull requests to watch.',
                       'Add a GitHub origin remote.')
    me = (github.gh_json(['api', 'user']) or {}).get('login')
    if not me and not all_authors:  # never fall back to running everyone's code
        raise cli.Fail(3, 'GH_FAILED', 'Could not find out which GitHub user you are, so EasyCI cannot tell your '
                       "pull requests from other people's.", 'Run: gh auth status (or pass --all-authors on purpose)')
    # Every page: a repository can have more than 100 open pull requests.
    pages = github.gh_json(['api', '--paginate', '--slurp', f'repos/{remote}/pulls?state=open&per_page=100'])
    pulls = None if pages is None else [pr for page in pages for pr in (page or [])]
    if pulls is None:
        raise cli.Fail(3, 'GH_FAILED', 'Could not list pull requests with gh.', 'Run: gh auth status')
    slug = store.project_slug(repo)
    state = store.read_json(state_path(slug)) or {}
    if isinstance(state.get('seen'), dict):
        seen, filtered = state['seen'], state.get('filtered') or {}
    else:  # the earlier flat {pr: sha} form
        seen, filtered = {k: v for k, v in state.items() if isinstance(v, str)}, {}
    out = []
    for pr in sorted(pulls, key=lambda p: p.get('number', 0)):
        head, base = pr.get('head') or {}, pr.get('base') or {}
        number, sha = str(pr.get('number')), head.get('sha')
        if not sha or seen.get(number) == sha:
            continue
        if str((head.get('repo') or {}).get('full_name') or '').lower() != remote.lower():  # GitHub names ignore case
            out.append({'pr': int(number), 'sha': sha, 'skipped': 'from a fork; never run automatically'})
            seen[number] = sha
            continue
        if not all_authors and (pr.get('user') or {}).get('login') != me:
            # Remembered apart from `seen`, with the account: --all-authors or another login runs it later.
            mark = f'{sha}@{me}'
            if filtered.get(number) != mark:
                out.append({'pr': int(number), 'sha': sha, 'skipped': f'opened by {pr["user"]["login"]}, not you'})
                filtered[number] = mark
            continue
        if not ensure_commit(repo, sha, head.get('ref')):
            out.append({'pr': int(number), 'sha': sha, 'skipped': 'head commit could not be fetched'})
            continue
        if base.get('sha') and not ensure_commit(repo, base['sha'], base.get('ref')):
            out.append({'pr': int(number), 'sha': sha, 'skipped': 'base commit could not be fetched'})
            continue
        # The commit pins what runs; the branch name feeds github.head_ref, as on GitHub.
        ns = argparse.Namespace(repo=str(repo), commit=sha, ref=head.get('ref'), base=base.get('sha'),
                                base_ref=base.get('ref'), event='pull_request',
                                profile=profile, job=None, workflow=None, backend=None, config=config, wait=False,
                                no_queue=False, no_report=no_report, keep=False, json=True)
        try:
            data = (start or cli.cmd_run)(ns)
            out.append({'pr': int(number), 'sha': sha, 'run_id': data['run_id']})
            seen[number] = sha
        except cli.Fail as error:
            item = {'pr': int(number), 'sha': sha, 'skipped': f'{error.code}: {error.message}'}
            if isinstance(error.data, dict) and error.data.get('run_id'):
                item['run_id'] = error.data['run_id']  # a run was made (e.g. UNSUPPORTED): do not queue it again
                seen[number] = sha
            out.append(item)
    open_numbers = {str(p.get('number')) for p in pulls}
    state = {'seen': {k: v for k, v in seen.items() if k in open_numbers},  # forget closed pull requests
             'filtered': {k: v for k, v in filtered.items() if k in open_numbers}}
    state_path(slug).parent.mkdir(parents=True, exist_ok=True)
    store.write_json(state_path(slug), state)
    return out


def sentence(item):
    if item.get('run_id') and not item.get('skipped'):
        return f'PR #{item["pr"]} has a new commit {item["sha"][:7]}: started run {item["run_id"]}.'
    return f'PR #{item["pr"]} ({item["sha"][:7]}) not run: {item["skipped"]}.'


def loop(repo, interval_minutes, say, cycles=None, sleep=time.sleep, **kwargs):
    """Check every interval. A GitHub failure (network down, gh timing out) is reported and the next
    check happens as usual; it never relaxes the author checks, since nothing runs in a failed cycle."""
    from easyci import cli
    done = 0
    while cycles is None or done < cycles:
        try:
            for item in cycle(repo, **kwargs):
                say(sentence(item))
        except cli.Fail as error:
            if error.code != 'GH_FAILED':
                raise
            say(f'GitHub could not be reached ({error.message}); trying again in {interval_minutes:g} minutes.')
        done += 1
        if cycles is None or done < cycles:
            sleep(max(60.0, interval_minutes * 60))
