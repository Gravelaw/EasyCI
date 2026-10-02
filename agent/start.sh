#!/usr/bin/env bash
set -euo pipefail
test -s /run/secrets/demo_agent_secret || { echo 'Approved enrollment secret required' >&2; exit 1; }
# Secret stays in the mounted file; never pass its contents in argv/environment/logs.
exec /usr/local/bin/jenkins-agent -url http://controller:8080/ -webSocket \
  -secret @/run/secrets/demo_agent_secret -name demo-linux -workDir /home/jenkins/agent
