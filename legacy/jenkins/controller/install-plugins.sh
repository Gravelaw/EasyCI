#!/usr/bin/env bash
set -euo pipefail
mkdir -p /usr/share/jenkins/ref/plugins
# TSV is generated from the reviewed JSON lock; the static check verifies equality.
while IFS=$'\t' read -r name url digest; do
  curl --fail --location --retry 2 --proto '=https' --proto-redir '=https' \
    "$url" -o "/usr/share/jenkins/ref/plugins/$name.jpi"
  printf '%s  %s\n' "$digest" "/usr/share/jenkins/ref/plugins/$name.jpi" | sha256sum -c -
done < /tmp/locked-plugins.tsv
rm /tmp/locked-plugins.tsv
