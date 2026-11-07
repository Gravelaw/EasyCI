"""Stand-in for the GitHub CLI. Records every call; never touches the network."""
import json
import os
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
