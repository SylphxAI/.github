"""Recognize literal private destinations and explicit in-cluster runtime calls.

This is a source regression check, not a deployment or authorization census.
Offsets refer to the original source; findings never expose source values.
"""
from __future__ import annotations

import pathlib
import re
import unicodedata

SOURCE = {'.rs', '.py', '.go', '.ts', '.tsx', '.jsx', '.js', '.mjs', '.cjs',
          '.sh', '.bash', '.java', '.lua', '.cs', '.kt', '.kts', '.swift', '.dart',
          '.c', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.m', '.mm', '.php', '.rb'}
CONFIG = {'.yaml', '.yml', '.toml', '.json', '.xml'}
MANIFEST_DIRS = {'deploy', 'deployment', 'deployments', 'infra', 'infrastructure',
                 'k8s', 'kubernetes', 'helm', 'charts', 'gitops', '.github'}
SUFFIX = re.compile(r'\.(?:svc(?:\.cluster\.local)?|cluster\.local|sylphx\.internal)(?![\w.-])', re.I)
PRIVATE_URL = re.compile(r'https?://([^/?#\s\"\x27`]+)', re.I)
URL = re.compile(r'\b[a-z][a-z0-9+.-]*://([^/?#\s\"\x27`]+)[^\s\"\x27`]*', re.I)
LITERAL = re.compile(r'\br(\#*)"[\s\S]*?"\1|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`(?:\\.|[^`\\])*`')
IN_CLUSTER = re.compile(r'\b(?:load_incluster_config|loadInClusterConfig|InClusterConfig|'
                        r'inClusterConfig|loadFromCluster)\s*\(|'
                        r'\b(?:Config|config)::incluster(?:_env|_dns)?\s*\(')
DIAGNOSTIC = re.compile(r'\b(?:error|warn|info|debug|trace|log|print|println|eprintln|'
                        r'anyhow|bail|expect|ok_or|Err)\s*!?\s*\([^()]*$', re.I)
MOUNT = re.compile(r'/var/run/secrets/kubernetes\.io/serviceaccount(?:/[^\s\"\x27`]*)?')


# Rust apostrophes introduce either one character or a lifetime/label, never an
# arbitrary single-quoted string. Raw delimiters and nested comments are lexical
# tokens: quotes, SQL strings and comment markers inside them have no effect.
RUST_RAW = re.compile(r'(?<![\w])(?:br|cr|r)(\#*)"')
RUST_CHAR = re.compile(r"'(?:[^'\\\n\r\t]|\\(?:[nrt\\0'\"]|x[0-9a-fA-F]{2}|u\{[0-9a-fA-F_]+\}))'")
_RUST_SCAN = re.compile(
    r"//[^\n]*"
    r"|/\*"
    r"|(?:br|cr|r)(\#*)\""
    r"|\"(?:[^\"\\]|\\[\s\S])*\""
    r"|(?P<unterm>\")"
    r"|'(?:[^'\\\n\r\t]|\\(?:[nrt\\0'\"]|x[0-9a-fA-F]{2}|u\{[0-9a-fA-F_]+\}))'"
)
_RUST_WORD = re.compile(r"\w")
_RUST_BLOCK_EDGE = re.compile(r"/\*|\*/")
_LITERAL_CANDIDATE = re.compile(
    r'(?i:https?://|\.(?:svc|cluster\.local|sylphx\.internal))'
    r'|kubernetes\.default'
    r'|/var/run/secrets/kubernetes\.io/serviceaccount'
    r'|KUBERNETES_SERVICE_(?:HOST|PORT)'
)
_HOST_PREFIX_END = re.compile(r'[\w}$.-]+$')
_TEMPLATE_PLUS_END = re.compile(r'\+\s*$')
_KUBE_DEFAULT = re.compile(r'\bkubernetes\.default\b')
_KUBE_ENV = re.compile(r'KUBERNETES_SERVICE_(?:HOST|PORT)')
_ENV_FUNC_PREFIX = re.compile(r'\b(?:var|getenv|env|environ|get_env|GetEnvironmentVariable)\b', re.I)
_NODE_KUBE_ENV = re.compile(r'\bprocess\.env\.KUBERNETES_SERVICE_(?:HOST|PORT)\b')
_SHELL_HTTP = re.compile(r'https?://[^\s;|&]+')
_SHORT_HOST = re.compile(r'[a-z][a-z0-9_-]*', re.I)
_VIEW_SUFFIXES = {'.sh', '.bash', '.rb', '.php', '.lua', '.yaml', '.yml', '.toml'}
_VIEW_TOKENS = {
    '.lua': re.compile(LITERAL.pattern + r'|--\[\[[\s\S]*?\]\]|--[^\n]*'),
    '.php': re.compile(LITERAL.pattern + r'|\#[^\n]*|//[^\n]*|/\*[\s\S]*?\*/'),
    'default': re.compile(LITERAL.pattern + r'|\#[^\n]*'),
}


def _blank_preserving_newlines(text):
    if '\n' not in text:
        return ' ' * len(text)
    return '\n'.join(' ' * len(part) for part in text.split('\n'))


def rust_tokens(text):
    """Yield (kind, start, end) tokens, preserving original source offsets."""
    size = len(text)
    pos = 0
    search = _RUST_SCAN.search
    edge_search = _RUST_BLOCK_EDGE.search
    word_match = _RUST_WORD.match
    while pos < size:
        match = search(text, pos)
        if match is None:
            break
        start = match.start()
        end = match.end()
        first = text[start]
        if first == '/':
            if text[start + 1] == '/':
                yield 'comment', start, end
                pos = end
            else:
                depth = 1
                pos = end
                while depth:
                    edge = edge_search(text, pos)
                    if edge is None:
                        raise ValueError('unterminated Rust block comment')
                    if text[edge.start()] == '/':
                        depth += 1
                    else:
                        depth -= 1
                    pos = edge.end()
                yield 'comment', start, pos
        elif match.group(1) is not None:
            if start > 0 and word_match(text, start - 1) is not None:
                pos = end - 1
                continue
            close = '"' + match.group(1)
            finish = text.find(close, end)
            if finish < 0:
                raise ValueError('unterminated Rust raw string')
            pos = finish + len(close)
            yield 'literal', start, pos
        elif match.group('unterm') is not None:
            raise ValueError('unterminated Rust string')
        else:
            yield 'literal', start, end
            pos = end


# This is deliberately not Rust name/type resolution. Only complete, recognized
# lexical expressions can prove that a literal is vocabulary rather than a
# destination. Unknown syntax or escaping uses keep the ordinary finding.
# Non-ASCII continuations include combining marks (Python \w is narrower than
# Rust XID_Continue). Group conservatively; unknown names cannot gain exclusions.
RUST_IDENTIFIER = re.compile(r'(?:[^\W\d]|[^\x00-\x7f\s\u200e\u200f])(?:\w|[^\x00-\x7f\s\u200e\u200f])*')
RUST_CODE_TOKEN = re.compile(r'::|==|!=|&&|\|\||=>|->|(?:r#)?' + RUST_IDENTIFIER.pattern + r'|[^\s\u200e\u200f]')
DIAGNOSTIC_ARGUMENT = {name: 0 for name in (
    'error', 'warn', 'info', 'debug', 'trace', 'print', 'println', 'eprintln',
    'anyhow', 'bail', 'panic')}
DIAGNOSTIC_ARGUMENT.update({'ensure': 1, 'assert': 1, 'assert_eq': 2, 'assert_ne': 2})
DIAGNOSTIC_FUNCTION_ARGUMENT = {'expect': 0, 'ok_or': 0, 'Err': 0}
EXPRESSION_MACROS = set(DIAGNOSTIC_ARGUMENT) | {'format', 'format_args', 'vec', 'json', 'matches'}
BUILTIN_ATTRIBUTES = {'cfg', 'allow', 'warn', 'deny', 'forbid', 'must_use', 'inline',
                      'cold', 'track_caller', 'repr'}
BUILTIN_DERIVES = {'Debug', 'Clone', 'Copy', 'PartialEq', 'Eq', 'Hash', 'Default', 'PartialOrd', 'Ord'}


class RustContext:
    """Balanced code/literal tokens; exclusions never cover a whole call body."""

    def __init__(self, clean, code, *, _literal_spans=None):
        tokens = [(m.start(), m.end(), m.group()) for m in RUST_CODE_TOKEN.finditer(code)]
        if _literal_spans is None:
            tokens += [(s, e, '<literal>') for kind, s, e in rust_tokens(clean) if kind == 'literal']
        else:
            tokens += [(s, e, '<literal>') for s, e, _ in _literal_spans]
        tokens.sort()
        self.values = [
            (s if s.isascii() else unicodedata.normalize('NFC', s))
            for _, _, v in tokens
            for s in (v.removeprefix('r#'),)
        ]
        self.literal_index = {s: i for i, (s, _, v) in enumerate(tokens) if v == '<literal>'}
        self.parent, self.close, self.argument_cache, stack = {}, {}, {}, []
        self.valid = True
        for i, v in enumerate(self.values):
            self.parent[i] = stack[-1] if stack else None
            if v in ('(', '[', '{'):
                stack.append(i)
            elif v in (')', ']', '}'):
                if not stack or self.values[stack[-1]] != {')': '(', ']': '[', '}': '{'}[v]:
                    self.valid = False
                    break
                self.close[stack.pop()] = i
        self.valid = self.valid and not stack
        self.attributes_known = self.valid and self.known_attributes()
        literal_values = [clean[s:e] for s, e, v in tokens if v == '<literal>']
        self.literal_names = {
            (name if name.isascii() else unicodedata.normalize('NFC', name))
            for value in literal_values
            for name in RUST_IDENTIFIER.findall(value)
        }
        # Escapes may spell a format capture without any matching source token.
        # Do not claim a closed use set without decoding that additional grammar.
        self.escaped_names = any('\\' in value for value in literal_values)
        self.vocabulary_constants = self.constants() if self.attributes_known else set()

    def arguments(self, opening):
        if opening in self.argument_cache:
            return self.argument_cache[opening]
        if opening not in self.close:
            return []
        segments, start, i = [], opening + 1, opening + 1
        while i < self.close[opening]:
            if self.values[i] == ',':
                segments.append((start, i))
                start = i + 1
            i = self.close.get(i, i) + 1
        if start < i:
            segments.append((start, i))
        self.argument_cache[opening] = segments
        return segments

    def diagnostic(self, i):
        opening = self.parent[i]
        if opening is None or self.values[opening] != '(':
            return False
        macro = opening > 0 and self.values[opening - 1] == '!'
        name = opening - 2 if macro else opening - 1
        table = DIAGNOSTIC_ARGUMENT if macro else DIAGNOSTIC_FUNCTION_ARGUMENT
        argument = table.get(self.values[name]) if name >= 0 else None
        if not macro and name >= 0 and self.values[name] in {'expect', 'ok_or'}:
            if name == 0 or self.values[name - 1] != '.':
                return False
        args = self.arguments(opening)
        return argument is not None and len(args) > argument and args[argument] == (i, i + 1)

    def known_attributes(self):
        v = self.values
        for i, value in enumerate(v[:-2]):
            if value != '#':
                continue
            opening = i + 2 if v[i + 1] == '!' else i + 1
            if v[opening] != '[' or opening not in self.close:
                return False
            if opening + 1 >= self.close[opening]:
                return False
            name = v[opening + 1]
            if v[opening + 2] not in {'(', '=', ']'}:
                return False
            if name == 'derive':
                if any(token not in BUILTIN_DERIVES | {'(', ')', ','}
                       for token in v[opening + 2:self.close[opening]]):
                    return False
            elif name not in BUILTIN_ATTRIBUTES:
                return False
        return True

    def known_context(self, i):
        v, opening = self.values, self.parent[i]
        while opening is not None:
            if (opening >= 2 and v[opening - 1] == '!' and
                    RUST_IDENTIFIER.fullmatch(v[opening - 2])):
                if v[opening - 2] not in DIAGNOSTIC_ARGUMENT:
                    return False
            if opening >= 3 and v[opening - 3:opening - 1] == ['macro_rules', '!']:
                return False
            opening = self.parent[opening]
        return True

    def comparison(self, i):
        if not self.known_context(i):
            return False
        v = self.values
        # A method's entire argument, not a nested constructor or conversion.
        opening = self.parent[i]
        if (opening is not None and opening >= 2 and v[opening] == '(' and
                v[opening - 1] in {'starts_with', 'ends_with'} and v[opening - 2] == '.' and
                self.arguments(opening) == [(i, i + 1)]):
            return True
        left = i - 1 if i and v[i - 1] == '&' else i
        before = v[left - 1] if left else None
        after = v[i + 1] if i + 1 < len(v) else None
        # No chained methods, concatenation, casts, qualified names or wrappers.
        if before in {'==', '!='}:
            return after in {None, ')', ']', '}', '{', ',', ';', '&&', '||', '=>'}
        if after in {'==', '!='}:
            return before in {None, '(', '[', '{', '}', ',', ';', '=', '=>', '!',
                              '&&', '||', 'return', 'if', 'while'}
        return False

    def constants(self):
        v, safe = self.values, set()
        # A private item is visible to child modules. Unread external children
        # and expansion-generated references cannot prove a closed use set.
        if self.escaped_names or 'macro_rules' in v or 'include' in v or any(
                (v[n] == 'mod' and n + 2 < len(v) and v[n + 2] == ';') or
                (v[n] == '!' and n and n + 1 < len(v) and v[n + 1] in {'(', '[', '{'} and
                 RUST_IDENTIFIER.fullmatch(v[n - 1]) and v[n - 1] not in EXPRESSION_MACROS) for n in range(len(v))):
            return safe
        uses = {}
        for i, value in enumerate(v):
            uses.setdefault(value, []).append(i)
        for i, value in enumerate(v):
            # Only an unexported, simple &str initializer. Associated/trait
            # constants and any shadowing, alias, return or macro escape refuse.
            if value != 'const' or i + 7 >= len(v):
                continue
            if i and v[i - 1] not in {'{', '}', ';', ']'}:
                continue
            if v[i + 2:i + 7] != [':', '&', 'str', '=', '<literal>'] or v[i + 7] != ';':
                continue
            parent, associated = self.parent[i], False
            while parent is not None:
                if v[parent] == '{':
                    header = parent - 1
                    while header >= 0 and v[header] not in {'{', '}', ';'}:
                        header -= 1
                    if set(v[header + 1:parent]) & {'impl', 'trait', 'macro_rules'}:
                        associated = True
                        break
                parent = self.parent[parent]
            references = [n for n in uses[v[i + 1]] if n != i + 1]
            # format!("{NAME}") implicitly captures a name without a code token.
            captured = v[i + 1] in self.literal_names
            if (not associated and not captured and references and
                    all(self.comparison(n) and (not n or v[n - 1] != '::') for n in references)):
                safe.add(i + 6)
        return safe

    def presence(self, i):
        v, opening = self.values, self.parent[i]
        if opening is None or v[opening] != '(' or self.arguments(opening) != [(i, i + 1)]:
            return False
        if opening < 5 or v[opening - 5:opening] not in (['std', '::', 'env', '::', 'var_os'],
                                         ['std', '::', 'env', '::', 'var']):
            return False
        # Do not confuse another qualified namespace with the standard library.
        before = opening - 6
        if before >= 0 and (v[before] == '.' or
                (v[before] == '::' and before > 0 and RUST_IDENTIFIER.fullmatch(v[before - 1]))):
            return False
        closing = self.close[opening]
        methods = {'is_some', 'is_none'} if v[opening - 1] == 'var_os' else {'is_ok', 'is_err'}
        return (v[closing + 1:closing + 3] in [['.', name] for name in methods] and
                v[closing + 3:closing + 5] == ['(', ')'])

    def environment_read(self, start):
        i = self.literal_index.get(start)
        opening = self.parent.get(i)
        return (opening is not None and opening > 0 and self.values[opening] == '(' and
                self.values[opening - 1] in {'var', 'var_os', 'getenv', 'get_env'})

    def non_destination(self, start):
        if not self.attributes_known or start not in self.literal_index:
            return False
        i = self.literal_index[start]
        return (self.known_context(i) and
                (self.diagnostic(i) or self.comparison(i) or
                 i in self.vocabulary_constants or self.presence(i)))


def literals(text, suffix, *, legacy_rust=False):
    if suffix == '.rs' and not legacy_rust:
        yield from ((start, end, text[start:end]) for kind, start, end in rust_tokens(text)
                    if kind == 'literal')
    else:
        yield from ((m.start(), m.end(), m.group()) for m in LITERAL.finditer(text))


def manifest(path):
    """Only manifest formats are allowed by deployment directory names."""
    p = pathlib.PurePosixPath(path)
    return (p.suffix in CONFIG and
            (bool({part.casefold() for part in p.parts[:-1]} & MANIFEST_DIRS)
             or p.name == 'sylphx.toml' or p.name.endswith('.deployment.profile.toml')))


def supported(path):
    return pathlib.PurePosixPath(path).suffix in SOURCE | CONFIG


def short_service_url(value):
    for match in PRIVATE_URL.finditer(value):
        # Authentication userinfo is not the destination hostname. This also
        # handles Git's configured credential URL without exposing its value.
        host = match.group(1).rsplit('@', 1)[-1].split(':', 1)[0]
        if _SHORT_HOST.fullmatch(host) and host.lower() != 'localhost':
            return True
    return False


def destination_host_span(value, start, end):
    """URL paths, queries and userinfo do not name the destination host."""
    for url in URL.finditer(value):
        if url.start() <= start < url.end():
            authority = url.group(1)
            host_start = url.start(1) + authority.rfind('@') + 1
            host = value[host_start:url.end(1)].split(':', 1)[0]
            return host_start <= start and end <= host_start + len(host)
    return True


def views(text, suffix, source_views):
    # Shell/Ruby/PHP/Lua comments differ from C-family // comments. Preserve
    # quoted and unquoted shell URLs while masking actual comments in place.
    if suffix in _VIEW_SUFFIXES:
        tokens = _VIEW_TOKENS.get(suffix, _VIEW_TOKENS['default'])
        clean_parts, code_parts = [], []
        last_clean = last_code = 0
        for match in tokens.finditer(text):
            start, end = match.start(), match.end()
            masked = _blank_preserving_newlines(match.group())
            code_parts.append(text[last_code:start])
            code_parts.append(masked)
            last_code = end
            if not LITERAL.fullmatch(match.group()):
                clean_parts.append(text[last_clean:start])
                clean_parts.append(masked)
                last_clean = end
        if not code_parts:
            return text, text
        code_parts.append(text[last_code:])
        if not clean_parts:
            return text, ''.join(code_parts)
        clean_parts.append(text[last_clean:])
        return ''.join(clean_parts), ''.join(code_parts)
    clean, code, _ = source_views(text, suffix)
    return clean, code


def scan(path, text, source_views, *, legacy_rust=False):
    if not supported(path) or manifest(path):
        return []
    lower = text.lower()
    if not any(token in lower for token in ('.svc', '.cluster.local', '.sylphx.internal',
                                             '://', 'incluster', 'loadfromcluster',
                                             'kubernetes_service_', 'kubernetes.io/serviceaccount')):
        return []
    suffix = pathlib.PurePosixPath(path).suffix
    if (suffix in {'.json', '.xml'}
            and '//' not in text and '/*' not in text
            and 'incluster' not in lower and 'loadfromcluster' not in lower
            and 'kubernetes_service_' not in lower):
        clean, code = text, ''
    else:
        clean, code = views(text, suffix, source_views)
    use_rust_ctx = suffix == '.rs' and not legacy_rust
    if not _LITERAL_CANDIDATE.search(clean):
        out = []
    else:
        file_literals = list(literals(clean, suffix, legacy_rust=legacy_rust)) if use_rust_ctx else None
        if use_rust_ctx:
            lit_iter = file_literals
        elif suffix == '.json':
            json_windows = []
            fallback_full = False
            size = len(clean)
            for m in _LITERAL_CANDIDATE.finditer(clean):
                ls = clean.rfind('\n', 0, m.start()) + 1
                le = clean.find('\n', m.end())
                if le < 0:
                    le = size
                if le - ls > 16384 or '"' not in clean[ls:m.start()] or '"' not in clean[m.end():le]:
                    fallback_full = True
                    break
                if json_windows and ls <= json_windows[-1][1]:
                    if le > json_windows[-1][1]:
                        json_windows[-1] = (json_windows[-1][0], le)
                else:
                    json_windows.append((ls, le))
            if fallback_full:
                lit_iter = ((m.start(), m.end(), None) for m in LITERAL.finditer(clean))
            else:
                lit_iter = (
                    (m.start(), m.end(), None)
                    for ws, we in json_windows
                    for m in LITERAL.finditer(clean, ws, we)
                )
        else:
            lit_iter = ((m.start(), m.end(), None) for m in LITERAL.finditer(clean))
        context = None
        out = []
        for start, end, value in lit_iter:
            if value is None:
                if not _LITERAL_CANDIDATE.search(clean, start, end):
                    continue
                value = clean[start:end]
            elif not _LITERAL_CANDIDATE.search(value):
                continue
            msg = None
            is_kube_env = False
            prefix = None
            for match in SUFFIX.finditer(value):
                if not destination_host_span(value, match.start(), match.end()):
                    continue
                before = value[:match.start()]
                # A suffix vocabulary or ends_with(".svc") refusal is not a host.
                # A concatenated suffix or template host still constructs a target.
                if not _HOST_PREFIX_END.search(before):
                    prefix = clean[max(0, start - 120):start]
                    if not _TEMPLATE_PLUS_END.search(prefix):
                        continue
                msg = 'Runtime source embeds cluster-private DNS; configure a reachable public API origin'
                break
            if msg is None:
                if short_service_url(value) or any(destination_host_span(value, m.start(), m.end())
                        for m in _KUBE_DEFAULT.finditer(value)):
                    msg = 'Runtime source embeds a single-label service or Kubernetes API hostname'
                elif MOUNT.search(value):
                    msg = 'Runtime source assumes the Kubernetes service-account secret mount'
                elif _KUBE_ENV.search(value):
                    is_kube_env = True
                    msg = 'Runtime source reads an in-cluster Kubernetes endpoint variable'
            if msg is None:
                continue
            if prefix is None:
                prefix = clean[max(0, start - 120):start]
            if use_rust_ctx:
                if context is None:
                    context = RustContext(clean, code, _literal_spans=file_literals)
                if context.non_destination(start):
                    continue
                if is_kube_env and not context.environment_read(start) and not _ENV_FUNC_PREFIX.search(prefix):
                    continue
            else:
                if DIAGNOSTIC.search(prefix):
                    continue
                if is_kube_env and not _ENV_FUNC_PREFIX.search(prefix):
                    continue
            out.append((start, end, msg))
    if code:
        for match in IN_CLUSTER.finditer(code):
            out.append((match.start(), match.end(), 'Runtime source explicitly constructs an in-cluster Kubernetes client'))
        for match in _NODE_KUBE_ENV.finditer(code):
            out.append((match.start(), match.end(), 'Runtime source reads an in-cluster Kubernetes endpoint variable'))
        if suffix in {'.sh', '.bash'}:
            # Shell destinations need not be quoted. Quoted ones are already masked.
            for match in _SHELL_HTTP.finditer(code):
                value = match.group()
                if any(destination_host_span(value, m.start(), m.end()) for m in SUFFIX.finditer(value)) or short_service_url(value):
                    out.append((match.start(), match.end(), 'Runtime shell command embeds a cluster-private service destination'))
    return sorted(set(out))
