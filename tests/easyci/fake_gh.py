"""Stand-in for the GitHub CLI. Records every call; never touches the network.

FAKE_GH_PULLS (JSON list) answers `api repos/<o>/<r>/pulls...`; FAKE_GH_COMMIT_PULLS answers
`api repos/<o>/<r>/commits/<sha>/pulls`; FAKE_GH_COMMENTS (a JSON file path) holds the PR comments,
which POST and PATCH calls update, so tests can read back what was written.
"""
import json
import os
import re
import sys

args = sys.argv[1:]
if args[:2] == ['auth', 'status']:
    if os.environ.get('FAKE_GH_AUTH', '0') != '0':
        print('You are not logged into any GitHub hosts. To log in, run: gh auth login', file=sys.stderr)
        sys.exit(1)
    print("github.com\n  Logged in to github.com account fake-user (keyring)\n  - Token scopes: 'gist', 'read:org', 'repo'")
    sys.exit(0)
if os.environ.get('FAKE_GH_LOG'):
    with open(os.environ['FAKE_GH_LOG'], 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(args) + '\n')
code = int(os.environ.get('FAKE_GH_EXIT', '0'))
if code:
    print('HTTP 502: Bad Gateway', file=sys.stderr)
    sys.exit(code)
if args and args[0] == 'api':
    method = args[args.index('--method') + 1] if '--method' in args else 'GET'
    path = next(a for a in args[1:] if not a.startswith('-') and args[args.index(a) - 1] not in ('--method', '-f'))
    fields = dict(args[i + 1].split('=', 1) for i, a in enumerate(args) if a == '-f')
    store = os.environ.get('FAKE_GH_COMMENTS')
    comments = json.load(open(store, encoding='utf-8')) if store and os.path.exists(store) else []
    if path == 'user':
        if os.environ.get('FAKE_GH_USER_FAILS'):
            sys.exit(1)
        print(json.dumps({'login': 'fake-user'}))
    elif re.fullmatch(r'repos/[^/]+/[^/]+/pulls(\?.*)?', path):
        source = os.environ.get('FAKE_GH_PULLS_FILE')  # for lists too long for an environment variable
        pulls = json.load(open(source, encoding='utf-8')) if source else json.loads(os.environ.get('FAKE_GH_PULLS', '[]'))
        if '--slurp' in args:  # one array per page of 100, as gh --paginate --slurp prints
            pulls = [pulls[i:i + 100] for i in range(0, len(pulls), 100)] or [[]]
        elif '--paginate' not in args:  # without --paginate the API returns the first page only
            pulls = pulls[:100]
        print(json.dumps(pulls))
    elif re.fullmatch(r'repos/[^/]+/[^/]+/commits/\w+/pulls', path):
        print(os.environ.get('FAKE_GH_COMMIT_PULLS', '[]'))
    elif re.fullmatch(r'repos/[^/]+/[^/]+/issues/\d+/comments(\?.*)?', path):
        number = int(path.split('/')[4])
        if method == 'POST':
            comments.append({'id': len(comments) + 1, 'issue': number, 'body': fields.get('body', '')})
            print(json.dumps(comments[-1]))
        else:
            mine = [c for c in comments if c['issue'] == number]
            if '--slurp' in args:  # pages of 100, as gh --paginate --slurp prints
                mine = [mine[i:i + 100] for i in range(0, len(mine), 100)] or [[]]
            elif '--paginate' not in args:
                mine = mine[:100]
            print(json.dumps(mine))
    elif re.fullmatch(r'repos/[^/]+/[^/]+/issues/comments/\d+', path) and method == 'PATCH':
        cid = int(path.rsplit('/', 1)[1])
        for comment in comments:
            if comment['id'] == cid:
                comment['body'] = fields.get('body', '')
        print('{}')
    else:
        print('{}')
    if store and method in ('POST', 'PATCH') and '/comments' in path:
        json.dump(comments, open(store, 'w', encoding='utf-8'))
sys.exit(0)
