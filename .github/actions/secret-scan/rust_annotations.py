"""Identify a narrow subset of unquoted Rust field and parameter type spans."""
from functools import lru_cache
import importlib.util
from pathlib import Path
import re

# Require multiple PascalCase words; arbitrary mixed alphanumeric tokens, all
# caps, and opaque identifiers are not evidence of a type. Allow a version
# digit within the name (e.g. SigV4Credentials), not arbitrary numeric runs.
_TYPE = re.compile(r'(?:[A-Z][a-z]+|[A-Z][0-9]){1,15}[A-Z][a-z]+')
_BINDING = re.compile(r'[A-Za-z_][A-Za-z_0-9]*')
_MATCH = re.compile(rb'([A-Za-z_][A-Za-z_0-9]*)[ \t]*:[ \t]*([A-Za-z0-9]+)[\s,})]?')
_CLOSE = {')': '(', '}': '{', ']': '['}


@lru_cache(maxsize=1)
def rust_lexer():
    # Use the pinned action checkout, never an import from the scanned repo.
    path = Path(__file__).resolve().parents[1] / 'stack-conformance/project_portability.py'
    spec = importlib.util.spec_from_file_location('secret_scan_rust_lexer', path)
    lexer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lexer)
    return lexer


def code_tokens(text):
    lexer = rust_lexer()
    previous = 0
    for kind, start, end in lexer.rust_tokens(text):
        yield from ((m.group(), m.start()) for m in lexer.RUST_CODE_TOKEN.finditer(text, previous, start))
        if kind == 'literal':
            yield '<literal>', start
        previous = end
    yield from ((m.group(), m.start()) for m in lexer.RUST_CODE_TOKEN.finditer(text, previous))


def type_spans(blob):
    """Return offsets for bare types in simple named struct fields or fn parameters."""
    # This proof is optional; large, invalid UTF-8, or ambiguous files keep the
    # original detector verdict. No Rust compiler or target code is invoked.
    if len(blob) > 1024 * 1024:
        return set()
    try:
        tokens = list(code_tokens(blob.decode('utf-8')))
    except ValueError:
        return set()
    values = [value for value, _ in tokens]
    # Declarative macro 2.0 can have several adjacent token trees. Do not
    # classify findings in files using that syntax.
    if 'macro' in values:
        return set()
    stack, spans = [], set()
    angles = 0
    for i, token in enumerate(values):
        if token in _CLOSE.values():
            kind = None
            # A macro or attribute token tree can contain arbitrary lookalikes.
            opaque = token == '[' or (bool(stack) and stack[-1][3]) or '!' in values[max(0, i - 2):i]
            if not opaque and i >= 2 and _BINDING.fullmatch(values[i - 1]):
                if token == '(' and values[i - 2] == 'fn':
                    kind = 'fn'
                elif token == '{' and values[i - 2] == 'struct':
                    kind = 'struct'
            stack.append((token, kind, i + 1, opaque, angles))
            angles = 0
        elif token in _CLOSE:
            if not stack or stack[-1][0] != _CLOSE[token]:
                return set()
            angles = stack.pop()[4]
        elif stack and stack[-1][1]:
            opening, kind, entry, opaque, outer_angles = stack[-1]
            if token == '<':
                angles += 1
            elif token == '>':
                angles = max(0, angles - 1)
            elif not angles and token == ',':
                stack[-1] = opening, kind, i + 1, opaque, outer_angles
            elif not angles and i >= entry + 2 and _TYPE.fullmatch(token):
                # Prove the entire binding, not just a line that looks like one.
                binding = i - 2
                modifier = 'mut' if kind == 'fn' else 'pub'
                closer = ')' if kind == 'fn' else '}'
                if (i + 1 < len(values) and values[i + 1] in (',', closer) and values[i - 1] == ':'
                        and _BINDING.fullmatch(values[binding])
                        and (binding == entry or (binding == entry + 1 and values[entry] == modifier))):
                    spans.add((tokens[binding][1], tokens[i][1]))
    return set() if stack else spans


def assignment_offsets(item, line):
    """Map one exact, unquoted Gitleaks match back to its Git source line."""
    column = item.get('StartColumn')
    if type(column) is not int or column < 2:
        return None
    # Git mode counts the patch's leading '+' in its one-based byte column.
    offset = column - 2
    match = _MATCH.match(line, offset)
    secret = item.get('Secret')
    if not match or not isinstance(secret, str) or not _TYPE.fullmatch(secret):
        return None
    if match.group(2).decode('ascii') != secret or match.group().decode('ascii') != item.get('Match'):
        return None
    return offset, match.start(2)
