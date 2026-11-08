"""The `${{ }}` expression subset of spec section 8.3: parse, validate, evaluate.

Anything outside the subset raises Unsupported with the expression text, never a guess.
"""
import math
import re


class Unsupported(Exception):
    pass


TOKEN = re.compile(r"\s*(?:(?P<num>-?\d+(?:\.\d+)?)\b|(?P<str>'(?:[^']|'')*')|"
                   r"(?P<op>==|!=|&&|\|\||<=|>=|[!()<>,.\[\]])|(?P<id>[A-Za-z_][A-Za-z0-9_-]*))")
FUNCTIONS = ('always', 'success', 'failure')
GITHUB = {('sha',), ('ref',), ('ref_name',), ('repository',), ('event_name',),
          ('event', 'pull_request', 'head', 'sha'), ('event', 'pull_request', 'base', 'sha')}
TEMPLATE = re.compile(r'\$\{\{(.*?)\}\}', re.S)


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
        node = self.not_()
        while self.peek() == ('op', '&&'):
            self.take()
            node = ('and', node, self.not_())
        return node

    def not_(self):
        if self.peek() == ('op', '!'):
            self.take()
            return ('not', self.not_())
        node = self.primary()
        if self.peek() in (('op', '=='), ('op', '!=')):
            op = self.take()[1]
            node = ('eq' if op == '==' else 'ne', node, self.primary())
        if self.peek()[1] in ('<', '>', '<=', '>=', '==', '!='):
            self.fail()
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
            self.take(')')
            if value not in FUNCTIONS:
                self.fail()
            return ('call', value)
        path = [value]
        while self.peek() == ('op', '.'):
            self.take()
            kind, name = self.take()
            if kind != 'id':
                self.fail()
            path.append(name)
        if self.peek()[1] == '[':
            self.fail()
        self.check_context(path)
        return ('ctx', tuple(path))

    def check_context(self, path):
        head, rest = path[0], tuple(path[1:])
        ok = ((head == 'github' and rest in GITHUB) or (head == 'env' and len(rest) == 1) or
              (head == 'runner' and rest == ('os',)) or
              (head == 'needs' and (len(rest) == 3 and rest[1] == 'outputs' or len(rest) == 2 and rest[1] == 'result')) or
              (head == 'steps' and len(rest) == 3 and rest[1] == 'outputs'))
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
    """Raise Unsupported if any ${{ }} in value (recursively) is outside the subset."""
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
    if node[0] == 'call':
        return True
    return node[0] in ('not', 'and', 'or', 'eq', 'ne') and any(uses_status(child) for child in node[1:])


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
        try:
            return float(value.strip())
        except ValueError:
            return math.nan
    return math.nan


def equal(a, b):
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if type(a) is type(b):
        return a == b
    return number(a) == number(b)


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
    return str(value)


def lookup(ctx, path):
    value = ctx
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def evaluate(node, ctx):
    kind = node[0]
    if kind == 'lit':
        return node[1]
    if kind == 'ctx':
        return lookup(ctx, node[1])
    if kind == 'call':
        status = ctx.get('status', 'success')
        return {'always': True, 'success': status == 'success', 'failure': status == 'failure'}[node[1]]
    if kind == 'not':
        return not truthy(evaluate(node[1], ctx))
    if kind == 'and':
        left = evaluate(node[1], ctx)
        return evaluate(node[2], ctx) if truthy(left) else left
    if kind == 'or':
        left = evaluate(node[1], ctx)
        return left if truthy(left) else evaluate(node[2], ctx)
    result = equal(evaluate(node[1], ctx), evaluate(node[2], ctx))
    return result if kind == 'eq' else not result


def condition(text, ctx, default='success()'):
    """Evaluate an `if:`; without a status function GitHub implies success() && (...)."""
    node = parse_condition(text if text not in (None, '') else default)
    if not uses_status(node):
        node = ('and', ('call', 'success'), node)
    return truthy(evaluate(node, ctx))


def interpolate(value, ctx):
    if isinstance(value, str):
        return TEMPLATE.sub(lambda m: to_str(evaluate(parse(m.group(1)), ctx)), value)
    if isinstance(value, dict):
        return {k: interpolate(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, ctx) for v in value]
    return value
