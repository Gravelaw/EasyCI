"""Read official metadata only; never pull images, install plugins or start services.

Run deliberately when reviewing a dependency refresh. The output is a proposal.
"""
import base64
import hashlib
import json
import pathlib
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]


def read(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=60) as r:
        return r.read(), dict(r.headers)


def image(repository, tag):
    # Anonymous, short-lived public registry pull authorization; not a user token.
    token = json.loads(read(f'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repository}:pull')[0])['token']
    data, headers = read(f'https://registry-1.docker.io/v2/{repository}/manifests/{tag}', {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json'})
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    assert headers.get('docker-content-digest', headers.get('Docker-Content-Digest')) == digest
    return f'{repository}:{tag}@{digest}'


if __name__ == '__main__':
    data = read('https://updates.jenkins.io/current/update-center.actual.json')[0]
    center = json.loads(data)
    roots = ['configuration-as-code', 'matrix-auth', 'workflow-cps', 'workflow-job',
             'workflow-basic-steps', 'workflow-durable-task-step', 'pipeline-input-step',
             'lockable-resources']
    selected = {}

    def include(name):
        if name in selected:
            return
        p = center['plugins'][name]
        if tuple(map(int, p['requiredCore'].split('.'))) > (2, 580, 1):
            raise ValueError(f'{name} requires {p["requiredCore"]}; review core baseline')
        selected[name] = {'version': p['version'], 'sha256': base64.b64decode(p['sha256']).hex(),
                          'url': p['url'], 'requiredCore': p['requiredCore']}
        for dependency in p['dependencies']:
            if not dependency['optional']:
                include(dependency['name'])

    for name in roots:
        include(name)
    lock = {'schemaVersion': 1, 'core': '2.580.1', 'metadataSha256': hashlib.sha256(data).hexdigest(),
            'metadataSource': 'https://updates.jenkins.io/current/update-center.actual.json',
            'roots': roots, 'plugins': dict(sorted(selected.items())),
            'images': {'controller': image('jenkins/jenkins', '2.580.1-jdk21'),
                       'agent': image('jenkins/inbound-agent', 'jdk21')}}
    (ROOT / 'dependency-lock.json').write_text(json.dumps(lock, indent=2) + '\n', encoding='utf-8', newline='\n')
    (ROOT / 'controller/plugins.txt').write_text(''.join(f'{k}:{v["version"]}\n' for k, v in lock['plugins'].items()), encoding='utf-8', newline='\n')
    (ROOT / 'controller/plugins-download.tsv').write_text(''.join(f'{k}\t{v["url"]}\t{v["sha256"]}\n' for k, v in lock['plugins'].items()), encoding='utf-8', newline='\n')
    print(f'Locked {len(selected)} plugins and two image indexes; no software installed.')
