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

    def test_negation_binds_tighter_than_equality(self):
        ctx = {**CTX, 'env': {'FLAG': 'nonempty'}}
        self.assertEqual(expr.parse('!env.FLAG == true')[0], 'eq')
        self.assertFalse(self.ev('!env.FLAG == true', ctx))   # (!'nonempty') == true -> false == true
        self.assertTrue(self.ev('!(env.FLAG == true)', ctx))
        self.assertTrue(self.ev('!env.MISSING', ctx))
        self.assertTrue(self.ev("!!env.FLAG && env.FLAG != 'x'", ctx))

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

    def test_phase1_functions_comparisons_and_indexing(self):
        ctx = {**CTX, 'matrix': {'os': 'ubuntu', 'node': 20, 'cfg': {'Mode': 'fast'}},
               'strategy': {'job-index': 1, 'job-total': 3}, 'env': {'LIST': '["a","b"]', 'X': 'yes'},
               'steps': {'s': {'outputs': {'v': '42'}, 'outcome': 'success'}},
               '__hash_files__': lambda patterns: 'h:' + ','.join(patterns)}
        ev = lambda text: self.ev(text, ctx)  # noqa: E731
        self.assertTrue(ev("contains(github.ref_name, 'FEAT')"))  # case-insensitive substring
        self.assertTrue(ev("contains(fromJSON(env.LIST), 'b')") and not ev("contains(fromJSON(env.LIST), 'c')"))
        self.assertTrue(ev("startsWith(github.ref_name, 'fea') && endsWith(github.ref_name, 'URE')"))
        self.assertEqual(ev("format('{0}-{1} {{x}}', matrix.os, matrix.node)"), 'ubuntu-20 {x}')
        self.assertEqual(ev("join(fromJSON(env.LIST), '+')"), 'a+b')
        self.assertEqual(ev("fromJSON('{\"a\": [1, 2]}').a[1]"), 2)
        self.assertEqual(ev("matrix['os']"), 'ubuntu')
        self.assertEqual(ev('matrix.cfg.mode'), 'fast')  # property names ignore case
        self.assertTrue(ev('matrix.node >= 18 && matrix.node < 22 && strategy.job-index < strategy.job-total'))
        self.assertTrue(ev("'b' > 'A'") and not ev("'abc' < 1"))  # strings compare case-insensitively; NaN is false
        self.assertEqual(ev("hashFiles('**/package-lock.json', 'x')"), 'h:**/package-lock.json,x')
        self.assertEqual(ev('steps.s.outcome'), 'success')
        self.assertEqual(expr.to_str(ev("toJSON(matrix.cfg)")), '{\n  "Mode": "fast"\n}')
        self.assertTrue(expr.condition('cancelled()', {**ctx, 'status': 'cancelled'}))
        self.assertFalse(expr.condition("contains(github.ref_name, 'feat')", {**ctx, 'status': 'failure'}))
        self.assertEqual(expr.evaluate_value("${{ fromJSON(env.LIST) }}", ctx), ['a', 'b'])
        self.assertEqual(expr.interpolate("${{ fromJSON(env.LIST) }}", ctx), '["a", "b"]')

    def test_unsupported_is_named_not_guessed(self):
        for text in ('secrets.TOKEN', 'vars.X', 'inputs.y', 'github.actor', 'github.event.pull_request.title',
                     'github.run_id', 'unknownFn(1)', "contains('a')", 'a < b < c', '1 < 2 < 3', 'github.event[0]',
                     'runner.foo', 'steps.s.bar', 'strategy.nope', 'job.services'):
            with self.subTest(text=text):
                with self.assertRaises(Unsupported) as ctx:
                    expr.parse(text)
                self.assertIn('unsupported_expression:', str(ctx.exception))
        with self.assertRaises(Unsupported):
            expr.check_template({'k': 'x ${{ secrets.GITHUB_TOKEN }}'})


if __name__ == '__main__':
    unittest.main()


class QuotedBraces(unittest.TestCase):
    def test_closing_braces_inside_strings_do_not_end_the_expression(self):
        # Review finding: `}}` inside a quoted argument cut the expression short.
        ctx = {'github': {}, 'env': {'X': 'v'}}
        text = "a ${{ fromJSON('{\"a\": {\"b\": 1}}').a.b }} b ${{ format('{{0}}-{0}', env.X) }} c"
        expr.check_template(text)
        self.assertEqual(expr.interpolate(text, ctx), 'a 1 b {0}-v c')
        self.assertEqual(expr.interpolate("${{ 'it''s }}' }}", ctx), "it's }}")
