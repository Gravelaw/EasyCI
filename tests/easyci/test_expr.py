import unittest

import ecsupport  # noqa: F401 - puts the repository root on sys.path
from easyci import expr
from easyci.expr import Unsupported

CTX = {'github': {'sha': 'abc', 'event_name': 'pull_request', 'ref_name': 'feature',
                  'event': {'pull_request': {'head': {'sha': 'abc'}}}},
       'needs': {'execution-policy': {'result': 'success', 'outputs': {'enabled': 'true'}}},
       'steps': {'s': {'outputs': {'v': '42'}}}, 'env': {'X': 'yes'}, 'runner': {'os': 'Linux'}, 'status': 'success'}


class ExpressionSubset(unittest.TestCase):
    def ev(self, text, ctx=CTX):
        return expr.evaluate(expr.parse(text), ctx)

    def test_supported_contexts_and_operators(self):
        self.assertTrue(self.ev("needs.execution-policy.outputs.enabled == 'true' && github.event_name == 'pull_request'"))
        self.assertEqual(self.ev('steps.s.outputs.v'), '42')
        self.assertEqual(self.ev('github.event.pull_request.head.sha'), 'abc')
        self.assertTrue(self.ev("!(env.X != 'yes') || false"))
        self.assertEqual(self.ev("env.MISSING || 'fallback'"), 'fallback')
        self.assertTrue(self.ev("runner.os == 'linux'"))  # string equality is case-insensitive
        self.assertTrue(self.ev("1 == '1'") and self.ev('null == 0') and self.ev('true == 1'))
        self.assertEqual(self.ev('needs.execution-policy.result'), 'success')

    def test_yaml_boolean_conditions(self):
        # Unquoted `if: true` / `if: false` reach the parser as Python bools (review finding).
        expr.parse_condition(True)
        expr.parse_condition(False)
        self.assertTrue(expr.condition(True, CTX))
        self.assertFalse(expr.condition(False, CTX))

    def test_status_functions_and_implicit_success(self):
        failed = {**CTX, 'status': 'failure'}
        self.assertFalse(expr.condition("env.X == 'yes'", failed))
        self.assertTrue(expr.condition('always()', failed))
        self.assertTrue(expr.condition('${{ failure() }}', failed))
        self.assertTrue(expr.condition(None, CTX))
        self.assertFalse(expr.condition(None, failed))

    def test_interpolation(self):
        self.assertEqual(expr.interpolate('v=${{ steps.s.outputs.v }} ${{ github.sha }} ${{ true }}', CTX), 'v=42 abc true')
        self.assertEqual(expr.interpolate({'a': ['${{ env.X }}']}, CTX), {'a': ['yes']})

    def test_unsupported_is_named_not_guessed(self):
        for text in ("contains(github.ref, 'x')", "startsWith(github.ref, 'r')", "format('{0}', 1)", 'toJSON(github)',
                     "hashFiles('**/x')", 'secrets.TOKEN', 'vars.X', 'inputs.y', 'github.actor', 'matrix.os',
                     "github.event.repository.default_branch", 'cancelled()', '1 < 2', "github['sha']", 'steps.s.outcome'):
            with self.subTest(text=text):
                with self.assertRaises(Unsupported) as ctx:
                    expr.parse(text)
                self.assertIn('unsupported_expression:', str(ctx.exception))
        with self.assertRaises(Unsupported):
            expr.check_template({'k': 'x ${{ secrets.GITHUB_TOKEN }}'})


if __name__ == '__main__':
    unittest.main()
