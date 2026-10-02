"""Reject regressions that grant unapproved users/groups control; no Jenkins service."""
import copy
import importlib.util
import pathlib
import unittest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('static', ROOT / 'scripts/check-static.py')
static = importlib.util.module_from_spec(spec); spec.loader.exec_module(static)


class AuthorizationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.final = yaml.safe_load((ROOT / 'controller/jenkins.yaml').read_text())
        self.bootstrap = yaml.safe_load((ROOT / 'controller/bootstrap.yaml').read_text())

    def test_explicit_user_only_final_policy_and_executorless_bootstrap(self):
        static.check_authorization(self.final, self.bootstrap)

    def test_authenticated_and_anonymous_group_admin_grants_rejected(self):
        for name in ('authenticated', 'anonymous'):
            altered = copy.deepcopy(self.final)
            altered['jenkins']['authorizationStrategy']['globalMatrix']['entries'].append(
                {'group': {'name': name, 'permissions': ['Overall/Administer']}})
            with self.assertRaises(AssertionError):
                static.check_authorization(altered, self.bootstrap)

    def test_other_admin_or_ambiguous_user_group_rejected(self):
        for entry in ({'user': {'name': 'ordinary-user', 'permissions': ['Overall/Administer']}},
                      {'userOrGroup': {'name': 'local-admin', 'permissions': ['Overall/Administer']}}):
            altered = copy.deepcopy(self.final)
            altered['jenkins']['authorizationStrategy']['globalMatrix']['entries'] = [entry]
            with self.assertRaises(AssertionError):
                static.check_authorization(altered, self.bootstrap)

    def test_bootstrap_cannot_enable_executors_or_weaken_auth(self):
        for key, value in (('numExecutors', 1), ('slaveAgentPort', 50000),
                           ('authorizationStrategy', {'unsecured': {}}), ('securityRealm', {'none': {}})):
            altered = copy.deepcopy(self.bootstrap); altered['jenkins'][key] = value
            with self.assertRaises(AssertionError):
                static.check_authorization(self.final, altered)


if __name__ == '__main__':
    unittest.main(verbosity=2)
