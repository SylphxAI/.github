"""GitHub's documented path-filter grammar (not shell/Python fnmatch).

The conformance vectors cite GitHub's primary cheat sheet. Unsupported syntax
is refused by the compiler rather than silently disagreeing with the runtime.
"""
import re


def pattern_regex(pattern):
    """Compile one positive pattern; ordered leading ! is handled separately."""
    tokens = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern[i:i + 3] == '**/':
            tokens.append('(?:.*/)?')
            i += 3
        elif pattern[i:i + 2] == '**':
            tokens.append('.*')
            i += 2
        elif char == '*':
            tokens.append('[^/]*')
            i += 1
        elif char in '?+':
            if not tokens or tokens[-1].endswith(('*', '?', '+')):
                raise ValueError(f'unsupported GitHub path quantifier: {pattern!r}')
            tokens[-1] = '(?:' + tokens[-1] + ')' + char
            i += 1
        elif char == '[':
            end = pattern.find(']', i + 1)
            content = pattern[i + 1:end]
            if end < 0 or not content or not re.fullmatch(r'[a-zA-Z0-9-]+', content):
                raise ValueError(f'unsupported GitHub path character class: {pattern!r}')
            try:
                re.compile('[' + content + ']')
            except re.error as error:
                raise ValueError(f'unsupported GitHub path character class: {pattern!r}') from error
            tokens.append('[' + content + ']')
            i = end + 1
        elif char == '\\':
            raise ValueError(f'unsupported GitHub path escape: {pattern!r}')
        else:
            tokens.append(re.escape(char))
            i += 1
    return re.compile(''.join(tokens))


def matches_path(patterns, path):
    """Apply include/exclude patterns in order to a whole repo-relative path."""
    selected = False
    for pattern in patterns:
        excluded = pattern.startswith('!')
        if pattern_regex(pattern[1:] if excluded else pattern).fullmatch(path):
            selected = not excluded
    return selected
