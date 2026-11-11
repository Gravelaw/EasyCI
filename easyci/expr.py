"""The `${{ }}` expression language of spec section 8.3 (Phase 1 scope): parse, validate, evaluate.

Supported: literals, `==` `!=` `<` `<=` `>` `>=` `!` `&&` `||`, parentheses, property access
(`a.b`, `a['b']`, `a[0]`), the status functions `success()` `failure()` `always()` `cancelled()`,
and `contains` `startsWith` `endsWith` `format` `join` `toJSON` `fromJSON` `hashFiles`.
Contexts: `github` (the keys EasyCI fills in), `env`, `runner`, `needs`, `steps`, `matrix`,
`strategy`, `job`. Anything else raises Unsupported with the expression text, never a guess.
"""
import json
import math
import re


class Unsupported(Exception):
    pass


TOKEN = re.compile(r"\s*(?:(?P<num>-?\d+(?:\.\d+)?)\b|(?P<str>'(?:[^']|'')*')|"
                   r"(?P<op>==|!=|&&|\|\||<=|>=|[!()<>,.\[\]*])|(?P<id>[A-Za-z_][A-Za-z0-9_-]*))")
STATUS = ('always', 'success', 'failure', 'cancelled')
# name: (min args, max args or None)
FUNCTIONS = {'contains': (2, 2), 'startsWith': (2, 2), 'endsWith': (2, 2), 'format': (1, None),
             'join': (1, 2), 'toJSON': (1, 1), 'fromJSON': (1, 1), 'hashFiles': (1, None)}
# Only keys EasyCI really knows; e.g. github.actor or a PR number would be a guess, so they stay unsupported.
GITHUB = {('sha',), ('ref',), ('ref_name',), ('ref_type',), ('head_ref',), ('base_ref',), ('repository',),
          ('repository_owner',), ('event_name',), ('server_url',), ('event', 'pull_request', 'head', 'sha'),
          ('event', 'pull_request', 'base', 'sha'), ('event', 'repository', 'default_branch')}
CONTEXTS = ('github', 'env', 'runner', 'needs', 'steps', 'matrix', 'strategy', 'job')
# `${{ ... }}`: the body may hold '...' strings (with '' escapes) containing `}}`, as in
# fromJSON('{"a": {"b": 1}}') or format('{{0}}', x); only a `}}` outside quotes ends it.
TEMPLATE = re.compile(r"\$\{\{((?:[^'}]|'(?:[^']|'')*'|\}(?!\}))*)\}\}", re.S)
COMPARE = {'==': 'eq', '!=': 'ne', '<': 'lt', '<=': 'le', '>': 'gt', '>=': 'ge'}


def tokenize(text):
    pos, out = 0, []
    while pos < len(text):
        if text[pos:].strip() == '':
            break
        match = TOKEN.match(text, pos)
        if not match:
            raise Unsupported(f'unsupported_expression:{text.strip()}')
        kind = match.lastgroup
        out.append((kind, match.group(kind)))
        pos = match.end()
    return out


class Parser:
    def __init__(self, text):
        self.text, self.tokens, self.i = text.strip(), tokenize(text), 0

    def fail(self):
        raise Unsupported(f'unsupported_expression:{self.text}')

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if tok[0] is None or (value is not None and tok[1] != value):
            self.fail()
        self.i += 1
        return tok

    def parse(self):
        node = self.or_()
        if self.i != len(self.tokens):
            self.fail()
        return node

    def or_(self):
        node = self.and_()
        while self.peek() == ('op', '||'):
            self.take()
            node = ('or', node, self.and_())
        return node

    def and_(self):
        node = self.comparison()
        while self.peek() == ('op', '&&'):
            self.take()
            node = ('and', node, self.comparison())
        return node

    def comparison(self):
        # `!` binds tighter than comparisons, as in GitHub: `!a == b` is `(!a) == b`.
        node = self.unary()
        if self.peek()[0] == 'op' and self.peek()[1] in COMPARE:
            op = self.take()[1]
            node = (COMPARE[op], node, self.unary())
        if self.peek()[0] == 'op' and self.peek()[1] in COMPARE:
            self.fail()  # chained comparisons are not supported
        return node

    def unary(self):
        if self.peek() == ('op', '!'):
            self.take()
            return ('not', self.unary())
        return self.postfix(self.primary())

    def postfix(self, node):
        path = list(node[1]) if node[0] == 'ctx' else None
        while self.peek() in (('op', '.'), ('op', '[')):
            if self.take()[1] == '.':
                kind, name = self.take()
                if kind != 'id':
                    self.fail()
                key = ('lit', name)
            else:
                key = self.or_()
                self.take(']')
            if path is not None and key[0] == 'lit' and isinstance(key[1], str):
                path.append(key[1])
                node = ('ctx', tuple(path))
            else:
                if path is not None:
                    self.check_context(tuple(path))  # a dynamic index needs a valid context in front of it
                path = None
                node = ('index', node, key)
        if node[0] == 'ctx':
            self.check_context(node[1])
        return node

    def primary(self):
        kind, value = self.take()
        if kind == 'num':
            return ('lit', float(value) if '.' in value else int(value))
        if kind == 'str':
            return ('lit', value[1:-1].replace("''", "'"))
        if (kind, value) == ('op', '('):
            node = self.or_()
            self.take(')')
            return node
        if kind != 'id':
            self.fail()
        if value in ('true', 'false', 'null'):
            return ('lit', {'true': True, 'false': False, 'null': None}[value])
        if self.peek() == ('op', '('):
            self.take()
            args = []
            if self.peek() != ('op', ')'):
                args.append(self.or_())
                while self.peek() == ('op', ','):
                    self.take()
                    args.append(self.or_())
            self.take(')')
            if value in STATUS and not args:
                return ('call', value)
            low, high = FUNCTIONS.get(value, (None, None))
            if low is None or len(args) < low or (high is not None and len(args) > high):
                self.fail()
            return ('fn', value, tuple(args))
        if value not in CONTEXTS:
            self.fail()
        return ('ctx', (value,))

    def check_context(self, path):
        head, rest = path[0], tuple(path[1:])
        ok = ((head == 'github' and rest in GITHUB) or (head == 'env' and len(rest) <= 1) or
              (head == 'runner' and rest in (('os',), ('arch',), ('temp',), ('name',))) or
              (head == 'needs' and (len(rest) >= 3 and rest[1] == 'outputs' or len(rest) == 2 and rest[1] == 'result')) or
              (head == 'steps' and (len(rest) == 3 and rest[1] == 'outputs' or
                                    len(rest) == 2 and rest[1] in ('outcome', 'conclusion'))) or
              head == 'matrix' or
              (head == 'strategy' and rest in (('job-index',), ('job-total',), ('fail-fast',), ('max-parallel',)))
              or (head == 'job' and rest == ('status',)))
        if not ok:
            self.fail()


def parse(text):
    return Parser(text).parse()


def condition_text(text):
    text = to_str(text).strip()  # YAML `if: true` arrives as a bool; str() would give 'True'
    match = TEMPLATE.fullmatch(text)
    return match.group(1) if match else text


def parse_condition(text):
    return parse(condition_text(text))


def check_template(value):
    """Raise Unsupported if any ${{ }} in value (recursively) is outside the language."""
    if isinstance(value, dict):
        for item in value.values():
            check_template(item)
    elif isinstance(value, list):
        for item in value:
            check_template(item)
    elif isinstance(value, str):
        for match in TEMPLATE.finditer(value):
            parse(match.group(1))


def uses_status(node):
    kind = node[0]
    if kind == 'call':
        return True
    if kind in ('lit', 'ctx'):
        return False
    if kind == 'fn':
        return any(uses_status(a) for a in node[2])
    return any(uses_status(child) for child in node[1:])


def number(value):
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if value.strip() == '':
            return 0
        text = value.strip()
        try:
            return int(text, 16) if text.lower().startswith('0x') else float(text)
        except ValueError:
            return math.nan
    return math.nan


def equal(a, b):
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    if type(a) is type(b):
        return a == b
    return number(a) == number(b)


def compare(kind, a, b):
    if isinstance(a, str) and isinstance(b, str):
        x, y = a.casefold(), b.casefold()
    else:
        x, y = number(a), number(b)
        if any(isinstance(v, float) and math.isnan(v) for v in (x, y)):
            return False
    return {'lt': x < y, 'le': x <= y, 'gt': x > y, 'ge': x >= y}[kind]


def truthy(value):
    if value is None or value is False or value == '':
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not math.isnan(value)
    return True


def to_str(value):
    if value is None:
        return ''
    if value is True or value is False:
        return 'true' if value else 'false'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def lookup(ctx, path):
    value = ctx
    for part in path:
        value = index(value, part)
    return value


def index(value, key):
    if isinstance(value, dict):
        if isinstance(key, str):
            for k, v in value.items():  # property names are case-insensitive in GitHub
                if isinstance(k, str) and k.casefold() == key.casefold():
                    return v
            return None
        return value.get(key)
    if isinstance(value, list) and isinstance(key, (int, float)) and not isinstance(key, bool):
        i = int(key)
        return value[i] if 0 <= i < len(value) else None
    return None


def fmt(template, args):
    out, i = [], 0
    while i < len(template):
        if template.startswith('{{', i):
            out.append('{')
            i += 2
        elif template.startswith('}}', i):
            out.append('}')
            i += 2
        elif template[i] == '{':
            end = template.find('}', i)
            if end < 0 or not template[i + 1:end].isdigit() or int(template[i + 1:end]) >= len(args):
                raise Unsupported(f'unsupported_expression:format({template!r})')
            out.append(to_str(args[int(template[i + 1:end])]))
            i = end + 1
        else:
            out.append(template[i])
            i += 1
    return ''.join(out)


def call(name, args, ctx):
    if name == 'contains':
        search, item = args
        if isinstance(search, list):
            return any(equal(v, item) for v in search)
        return to_str(item).casefold() in to_str(search).casefold()
    if name == 'startsWith':
        return to_str(args[0]).casefold().startswith(to_str(args[1]).casefold())
    if name == 'endsWith':
        return to_str(args[0]).casefold().endswith(to_str(args[1]).casefold())
    if name == 'format':
        return fmt(to_str(args[0]), args[1:])
    if name == 'join':
        sep = to_str(args[1]) if len(args) > 1 else ','
        return sep.join(to_str(v) for v in args[0]) if isinstance(args[0], list) else to_str(args[0])
    if name == 'toJSON':
        return json.dumps(args[0], indent=2)
    if name == 'fromJSON':
        try:
            return json.loads(to_str(args[0]))
        except ValueError:
            raise Unsupported(f'unsupported_expression:fromJSON({to_str(args[0])[:60]!r})') from None
    if name == 'hashFiles':
        hasher = ctx.get('__hash_files__')
        return hasher([to_str(a) for a in args]) if hasher else ''
    raise Unsupported(f'unsupported_expression:{name}()')


def evaluate(node, ctx):
    kind = node[0]
    if kind == 'lit':
        return node[1]
    if kind == 'ctx':
        return lookup(ctx, node[1])
    if kind == 'index':
        return index(evaluate(node[1], ctx), evaluate(node[2], ctx))
    if kind == 'call':
        status = ctx.get('status', 'success')
        return {'always': True, 'success': status == 'success', 'failure': status == 'failure',
                'cancelled': status == 'cancelled'}[node[1]]
    if kind == 'fn':
        return call(node[1], [evaluate(a, ctx) for a in node[2]], ctx)
    if kind == 'not':
        return not truthy(evaluate(node[1], ctx))
    if kind == 'and':
        left = evaluate(node[1], ctx)
        return evaluate(node[2], ctx) if truthy(left) else left
    if kind == 'or':
        left = evaluate(node[1], ctx)
        return left if truthy(left) else evaluate(node[2], ctx)
    a, b = evaluate(node[1], ctx), evaluate(node[2], ctx)
    if kind in ('eq', 'ne'):
        return equal(a, b) if kind == 'eq' else not equal(a, b)
    return compare(kind, a, b)


def condition(text, ctx, default='success()'):
    """Evaluate an `if:`; without a status function GitHub implies success() && (...)."""
    node = parse_condition(text if text not in (None, '') else default)
    if not uses_status(node):
        node = ('and', ('call', 'success'), node)
    return truthy(evaluate(node, ctx))


def evaluate_value(value, ctx):
    """Like interpolate, but a string that is exactly one `${{ }}` keeps the expression's type
    (so `matrix: ${{ fromJSON(...) }}` yields a mapping). Lists and mappings are walked."""
    if isinstance(value, str):
        full = TEMPLATE.fullmatch(value.strip())
        if full:
            return evaluate(parse(full.group(1)), ctx)
        return interpolate(value, ctx)
    if isinstance(value, dict):
        return {k: evaluate_value(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [evaluate_value(v, ctx) for v in value]
    return value


def interpolate(value, ctx):
    if isinstance(value, str):
        return TEMPLATE.sub(lambda m: to_str(evaluate(parse(m.group(1)), ctx)), value)
    if isinstance(value, dict):
        return {k: interpolate(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, ctx) for v in value]
    return value
