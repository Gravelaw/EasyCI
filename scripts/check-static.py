"""Bounded static validation using already installed PyYAML/jsonschema; no network."""
import importlib.util
import json
import pathlib
import re
import sys
import yaml
import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pipelines', ROOT / 'scripts/render-pipelines.py')
pipelines = importlib.util.module_from_spec(spec); spec.loader.exec_module(pipelines)


def check_authorization(casc, bootstrap):
    # Exact user-typed entry: no authenticated/anonymous group, wildcard, fallback,
    # other administrator or ambiguity between a user and a group of this name.
    assert casc['jenkins']['authorizationStrategy'] == {
        'globalMatrix': {'entries': [{'user': {'name': 'local-admin', 'permissions': ['Overall/Administer']}}]}}
    assert 'securityRealm' not in casc['jenkins']
    assert 'authorizationStrategy' not in bootstrap['jenkins']
    assert 'securityRealm' not in bootstrap['jenkins']
    for configuration in (casc, bootstrap):
        assert configuration['jenkins']['numExecutors'] == 0
        assert configuration['jenkins']['slaveAgentPort'] == -1


def check():
    compose = yaml.safe_load((ROOT / 'compose.yaml').read_text())
    casc = yaml.safe_load((ROOT / 'controller/jenkins.yaml').read_text())
    bootstrap = yaml.safe_load((ROOT / 'controller/bootstrap.yaml').read_text())
    bootstrap_compose = yaml.safe_load((ROOT / 'compose.bootstrap.yaml').read_text())
    lock = json.loads((ROOT / 'dependency-lock.json').read_text())
    schema = json.loads((ROOT / 'schemas/project.schema.json').read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    for project in ('demo', 'juliaflow'):
        p = json.loads((ROOT / f'projects/{project}.json').read_text())
        jsonschema.validate(p, schema)
        assert pipelines.render(project) == (ROOT / f'pipelines/{project}.Jenkinsfile').read_text()
    check_authorization(casc, bootstrap)
    assert bootstrap_compose == {'services': {'controller': {'environment': {
        'CASC_JENKINS_CONFIG': '/usr/share/jenkins/ref/bootstrap.yaml'}}}}
    assert compose['services']['controller']['ports'] == ['127.0.0.1:8080:8080']
    for name, service in compose['services'].items():
        assert service['restart'] == 'no' and service['cap_drop'] == ['ALL']
        assert service['security_opt'] == ['no-new-privileges:true']
        assert service['cpus'] > 0 and service['mem_limit'] and service['pids_limit'] > 0
        assert not service.get('privileged') and service.get('network_mode') != 'host'
        assert not service.get('devices') and not service.get('pid')
        for volume in service.get('volumes', []):
            assert volume.split(':')[0] in compose['volumes'], 'Only named volumes permitted by default'
        if name != 'controller':
            assert not service.get('ports')
    assert 'docker.sock' not in (ROOT / 'compose.yaml').read_text()
    assert compose['services']['demo-agent']['profiles'] == ['demo']
    for name, image in lock['images'].items():
        assert re.fullmatch(r'jenkins/[a-z-]+:[a-zA-Z0-9.-]+@sha256:[0-9a-f]{64}', image)
        dockerfile = ROOT / ('controller/Dockerfile' if name == 'controller' else 'agent/Dockerfile')
        assert dockerfile.read_text().splitlines()[0] == 'FROM ' + image
    assert set(lock['roots']) <= set(lock['plugins'])
    assert 'matrix-auth' in lock['roots']
    assert 'git' not in lock['plugins'] and 'docker-plugin' not in lock['plugins']
    expected = ''.join(f'{k}:{v["version"]}\n' for k, v in lock['plugins'].items())
    assert (ROOT / 'controller/plugins.txt').read_text() == expected
    expected = ''.join(f'{k}\t{v["url"]}\t{v["sha256"]}\n' for k, v in lock['plugins'].items())
    assert (ROOT / 'controller/plugins-download.tsv').read_text() == expected
    for path in [ROOT / 'controller/plugins-download.tsv', ROOT / 'controller/install-plugins.sh', ROOT / 'agent/start.sh']:
        assert b'\r' not in path.read_bytes(), 'Docker shell inputs must use LF on Windows too'
    for name, p in lock['plugins'].items():
        assert re.fullmatch(r'[0-9a-f]{64}', p['sha256'])
        assert p['url'] == f'https://updates.jenkins.io/download/plugins/{name}/{p["version"]}/{name}.hpi'
        assert tuple(map(int, p['requiredCore'].split('.'))) <= (2, 580, 1)
    # Secret provisioning and protection-bypass patterns may never enter executable configuration.
    for path in [ROOT / 'controller/Dockerfile', ROOT / 'controller/jenkins.yaml', ROOT / 'compose.yaml']:
        text = path.read_text()
        for banned in ('ALLOW_LOCAL_CHECKOUT', 'runSetupWizard=false', 'csrfProtection: null', 'apiToken:', 'password:'):
            assert banned not in text
    print(f'PASS: YAML policy, schema, pipeline generation, {len(lock["plugins"])} plugin pins and image digest consistency')


if __name__ == '__main__':
    check()
