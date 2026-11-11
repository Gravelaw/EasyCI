"""Phase 3 planning of `container:` and `services:` for the podman backend (no Podman needed here)."""
import unittest

import ecsupport  # noqa: F401 - puts the repository root on sys.path
from easyci import workflow
from easyci.backends.podman import object_name, parse_options, parse_ports

INP = {'event': 'push', 'ref_name': 'main', 'default_branch': 'main', 'commit': 'a' * 40, 'base': 'b' * 40,
       'repository': 'o/r'}
JOB = """\
on: push
jobs:
  t:
    runs-on: ubuntu-latest
    container: node:20-alpine
    services:
      cache:
        image: redis:7-alpine
        ports: ['6379']
        options: >-
          --health-cmd "redis-cli ping" --health-interval 2s --health-timeout 2s --health-retries 10
    steps:
      - run: echo ${{ job.services.cache.ports['6379'] }}
"""


def plan(text, cfg=None, **inp):
    return workflow.build_plan([('.github/workflows/ci.yml', text.encode())], cfg or {}, {**INP, **inp})['jobs'][0]


class ContainerPlanning(unittest.TestCase):
    def test_needs_a_podman_backend(self):
        job = plan(JOB)
        self.assertEqual(job['predicted'], 'unsupported')
        self.assertTrue(job['predicted_reason'].startswith('unsupported_container:container/services need a podman'))

    def test_container_backend_takes_container_jobs(self):
        job = plan(JOB, {'container_backend': 'podman:boxes'})
        self.assertEqual((job['predicted'], job['backend']), ('run', 'podman:boxes'))
        self.assertEqual(job['container'], {'image': 'node:20-alpine', 'env': {}, 'ports': [], 'options': ''})
        self.assertEqual(job['gh_services']['cache']['image'], 'redis:7-alpine')
        self.assertIn('--health-cmd', job['gh_services']['cache']['options'])
        self.assertEqual(plan(JOB, backend='podman:x')['backend'], 'podman:x')

    def test_unsupported_container_settings_are_named(self):
        cases = {'    container: {image: x, volumes: [a:b]}\n': 'unsupported_container:container.volumes',
                 '    container: {image: x, credentials: {username: u}}\n': 'unsupported_container:container.credentials',
                 '    container: {image: x, options: --privileged}\n': 'unsupported_container:container options --privileged',
                 '    container: {env: {A: 1}}\n': 'unsupported_container:container needs an image'}
        for line, reason in cases.items():
            with self.subTest(line=line):
                text = 'on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n' + line + '    steps: [{run: x}]\n'
                job = plan(text, {'container_backend': 'podman:boxes'})
                self.assertEqual(job['predicted_reason'], reason)

    def test_option_and_port_parsing(self):
        self.assertEqual(parse_options('--health-cmd "pg_isready -U x" --health-interval=5s'),
                         ['--health-cmd', 'pg_isready -U x', '--health-interval', '5s'])
        self.assertEqual(parse_options(''), [])
        with self.assertRaises(ValueError):
            parse_options('--health-retries 3 --cpus 2')
        self.assertEqual(parse_ports(['5432:5432', 6379, '8080/tcp', ':9000']),
                         [('5432', '5432'), ('6379', None), ('8080', None), ('9000', None)])
        self.assertTrue(workflow.BACKEND.match('podman:easyci-containers'))
        with self.assertRaises(workflow.ConfigError):
            workflow.load_config('container_backend: wsl:x\n')


class Names(unittest.TestCase):
    def test_long_names_keep_services_apart(self):
        # Review finding: truncating to 63 characters gave two services the same container name.
        base = 'easyci-20261111-194847-ff8b-integration-tests-with-long-name-123456'[:60]
        a, b = object_name(base, 'redis-primary'), object_name(base, 'redis-secondary')
        self.assertNotEqual(a, b)
        self.assertTrue(all(len(n) <= 63 and n.startswith('easyci-20261111-194847-ff8b') for n in (a, b)))
        self.assertEqual(object_name('easyci-x-y', 'db'), 'easyci-x-y-db')
