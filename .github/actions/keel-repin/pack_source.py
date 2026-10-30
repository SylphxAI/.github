"""Find Docker stages that pack Keel source without importing its build identity.

Shared by the repin repair and stack check. Reads tracked text through a caller
supplied function; it never executes a title's scripts.
"""

import json
import posixpath
import re
import shlex

ARG = "SYLPHX_GIT_COMMIT_SHA"
SCRIPT = re.compile(r"(?:[\w.-]+/)*[\w.-]+\.(?:sh|py|mjs|js)$")
KEEL = re.compile(r"(?:.*/)?keel|\$\{?[Kk][Ee][Ee][Ll][\w]*(?::-[^}]+)?\}?")
SKIP = frozenset({"vendor", "target", "node_modules", "dist", "build", ".git"})


def dockerfile(path):
    name = posixpath.basename(path)
    return (not SKIP.intersection(path.split("/")[:-1])
            and (name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile"))
            and not name.endswith(".dockerignore"))


def commands(text):
    """Shell commands, preserving quoted arguments and ignoring comments."""
    # shlex's default comment handling truncates ${value#pattern}, a common
    # wrapper expansion. Shell comments begin a word outside quotes instead.
    chars, quote, escaped, comment = [], "", False, False
    for index, char in enumerate(text):
        if comment and char != "\n":
            continue
        if char == "\n":
            comment = False
        if not quote and not escaped and char == "#" and (index == 0 or text[index - 1] in " \t\r\n;&|()"):
            comment = True
            continue
        chars.append(char)
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char == quote:
            quote = ""
        elif char in "\"'" and not quote:
            quote = char
    lexer = shlex.shlex("".join(chars).replace("\\\n", " "), posix=True, punctuation_chars=";&|()\n")
    lexer.commenters = ""
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    words = []
    try:
        for token in lexer:
            if token and all(c in ";&|()\n" for c in token):
                if words:
                    yield words
                words = []
            else:
                words.append(token)
    except ValueError:
        # A script in another language can have shell-unbalanced quotes. Keep
        # complete commands already read; it is never run by this recognizer.
        pass
    if words:
        yield words


def packs(text, path, paths, read, seen=frozenset()):
    # A wrapper may keep pack's arguments in a literal Bash array. Resolve only
    # that static form; no script evaluation or command substitution occurs.
    arrays = re.findall(r"\b([A-Za-z_]\w*)=\(\s*(pack(?:\s[^)]*)?)\)", text)
    for name, arguments in arrays:
        text = text.replace('"${' + name + '[@]}"', arguments)
        text = text.replace('${' + name + '[@]}', arguments)
    for words in commands(text):
        while words and (words[0] in {"if", "then", "do", "elif", "exec", "env", "command"}
                         or words[0].startswith(("--mount=", "--network=", "--security="))
                         or re.match(r"^[A-Za-z_]\w*=", words[0])):
            words = words[1:]
        if not words:
            continue
        executable = posixpath.basename(words[0])
        if KEEL.fullmatch(words[0]) and len(words) > 1 and words[1] == "pack":
            if any(word in {"--verify", "--list-profiles", "--update-keygen"}
                   or word.startswith("--verify=") for word in words[2:]):
                continue
            profile = next((word.split("=", 1)[1] for word in words[2:] if word.startswith("--profile=")), None)
            for index, word in enumerate(words[2:], 2):
                if word in {"--profile", "-p"} and index + 1 < len(words):
                    profile = words[index + 1]
            if profile is None or profile == "web" or "$" in profile:
                return True
            continue
        # A shell's command argument is executable text, unlike echo/printf.
        if executable in {"sh", "bash", "dash"} and "-c" in words:
            index = words.index("-c") + 1
            if index < len(words) and packs(words[index], path, paths, read, seen):
                return True
        candidates = words[1:] if executable in {"sh", "bash", "dash", "python", "python3", "node", "bun"} else words[:1]
        for word in candidates:
            if not SCRIPT.fullmatch(word):
                continue
            for name in (posixpath.normpath(word), posixpath.normpath(posixpath.join(posixpath.dirname(path), word))):
                if name in paths and name not in seen:
                    source = read(name)
                    if source is not None and packs(source, name, paths, read, seen | {name}):
                        return True
    return False


def instructions(text):
    """Yield (first line, last line, instruction, argument) for Docker text."""
    pending, first = "", 0
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not pending:
            first = number
        pending += line.rstrip().removesuffix("\\") + " "
        if line.rstrip().endswith("\\"):
            continue
        match = re.match(r"\s*([A-Za-z]+)\s+(.*)", pending, re.S)
        if match:
            yield first, number, match[1].upper(), match[2].strip()
        pending = ""


def missing_stages(text, path, paths, read):
    """Return (FROM end line, pack line, stage) once per unidentified stage.

    Global ARG is out of stage scope. A FROM of an earlier named stage inherits
    that stage's ARGs; an unrelated FROM does not. A later ARG cannot identify
    an earlier pack invocation.
    """
    stages, current, found, stage_index = {}, None, [], 0
    for first, last, instruction, argument in instructions(text):
        if instruction == "FROM":
            words = shlex.split(argument)
            words = [word for word in words if not word.startswith("--")]
            if not words:
                continue
            name = words[2] if len(words) > 2 and words[1].upper() == "AS" else str(first)
            current = {"line": last, "name": name, "arg": stages.get(words[0].lower(), {}).get("arg", False), "found": False}
            stages[name.lower()] = current
            stages[str(stage_index)] = current
            stage_index += 1
        elif current is not None and instruction == "ARG" and argument.split("=", 1)[0].strip() == ARG:
            current["arg"] = True
        elif current is not None and instruction == "RUN" and not current["arg"] and not current["found"]:
            if argument.startswith("["):
                try:
                    argument = shlex.join(json.loads(argument))
                except (ValueError, TypeError):
                    continue
            if packs(argument, path, paths, read):
                found.append((current["line"], first, current["name"]))
                current["found"] = True
    return found
