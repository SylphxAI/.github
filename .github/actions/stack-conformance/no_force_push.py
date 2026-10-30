"""Recognize force-push commands without executing repository code.

Findings contain a digest of the literal command, never its arguments: remote
URLs can contain credentials. Exact occurrences form a shrinking baseline.
"""
from __future__ import annotations

import ast
import collections
import hashlib
import json
import pathlib
import re
import shlex
from urllib.parse import quote

BASELINE_MARKER = "# force-push-ratchet-v1"
CONTRACT = "https://github.com/SylphxAI/owner/blob/main/AGENTS.md"
SHELL = {".sh", ".bash", ".zsh", ".ksh", ".ps1", ".cmd", ".bat"}
SOURCE = {".py", ".js", ".ts", ".mjs", ".cjs", ".rb", ".pl"}
SKIP = {"node_modules", "vendor", "third_party", "thirdparty", "external", ".git"}
LIMITS = [
    "Recognizes literal Git pushes, shell forwarding helpers and forced GitHub branch-ref PATCH calls in scripts, workflow/action run blocks and package scripts.",
    "Dynamically constructed executables, options and refspecs require semantic review; a passing static check is not a proof about arbitrary runtime code.",
]
EXECUTORS = {"run", "Popen", "call", "check_call", "check_output", "system", "execv", "execvp", "execve", "execl", "execlp"}


def unsafe_push(argv):
    """Return a reason for a literal Git push argv, otherwise None."""
    if not argv or pathlib.PurePosixPath(argv[0]).name.lower() not in {"git", "git.exe"}:
        return None
    i = 1
    while i < len(argv) and argv[i] != "push":
        arg = argv[i]
        if arg in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return None  # Another Git subcommand, such as fetch.
    if i == len(argv):
        return None
    options = True
    forced = None
    destinations = []
    remote_seen = False
    i += 1
    while i < len(argv):
        arg = argv[i]
        if options and arg == "--":
            options = False
        elif options and arg in {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}:
            if arg == "--repo":
                remote_seen = True
            i += 1  # An option's value is not itself a push option/refspec.
        elif options and arg == "--mirror":
            return "mirror push rewrites branches"
        elif options and (arg.split("=", 1)[0] in {"--force", "--force-with-lease", "--force-if-includes"}
                          or re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", arg)):
            forced = "force option"
        elif not (options and arg.startswith("-")):
            if remote_seen:
                destinations.append(arg)
            else:
                remote_seen = True
        i += 1
    # Explicit tag-only refspecs cannot rewrite branch history. Unqualified
    # destinations and mixed branch/tag pushes remain subject to the rule.
    tags_only = bool(destinations) and all(arg.lstrip("+").rsplit(":", 1)[-1].startswith("refs/tags/")
                                         for arg in destinations)
    if tags_only:
        return None
    return forced or ("forced refspec" if any(arg.startswith("+") for arg in destinations) else None)


def shell_commands(text, line=1):
    """Yield argv and source lines, retaining newlines as command boundaries."""
    tokens = re.compile(r'''(?P<space>[ \t\r]+|\\\n)|(?P<comment>\#[^\n]*)|(?P<separator>[;&|()\n]+)|(?P<word>(?:\\[\s\S]|'[^']*'|"(?:\\[\s\S]|[^"\\])*"|[^\s;&|()'"\\])+)''')
    command, start = [], line
    position = 0
    for match in tokens.finditer(text):
        if match.start() != position:
            raise ValueError("unterminated command quoting")
        position = match.end()
        if match.lastgroup in {"space", "comment"}:
            continue
        if match.lastgroup == "separator":
            if command:
                yield command, start
            command = []
            continue
        if not command:
            start = line + text.count("\n", 0, match.start())
        command.append(shlex.split(match[0].replace("\\\n", ""), comments=False)[0])
    if position != len(text):
        raise ValueError("unterminated command quoting")
    if command:
        yield command, start


def executable_commands(text, line=1):
    masked, heredocs = shell_heredocs(text)
    for body, at, quoted, launcher in heredocs:
        if re.search(r"\bpython[0-9.]*\s", launcher):
            for argv, offset in python_commands(body):
                yield argv, line + at + offset - 1
        elif re.search(r"\b(?:bash|sh)\s", launcher):
            yield from executable_commands(body, line + at)
        elif not quoted:
            # A data heredoc is not a script; only its substitutions execute.
            _, expansions = command_substitutions(body)
            for script, offset in expansions:
                yield from executable_commands(script, line + at + offset)
    masked, substitutions = command_substitutions(masked)
    for body, at in substitutions:
        yield from executable_commands(body, line + at)
    for argv, at in shell_commands(masked, line):
        while argv and (argv[0] in {"if", "then", "elif", "do", "!", "command", "exec", "env", "sudo"}
                        or re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", argv[0])):
            argv = argv[1:]
        if not argv:
            continue
        yield argv, at
        if pathlib.PurePosixPath(argv[0]).name in {"sh", "bash", "zsh", "ksh"} and "-c" in argv:
            index = argv.index("-c") + 1
            if index < len(argv):
                yield from executable_commands(argv[index], at)


def shell_heredocs(text):
    lines = text.splitlines(True)
    masked, found, i = list(lines), [], 0
    while i < len(lines):
        match = re.search(r'''(?<!<)<<(-?)(['"]?)([A-Za-z_][A-Za-z_0-9]*)\2''', lines[i])
        if not match:
            i += 1
            continue
        end = i + 1
        while end < len(lines) and lines[end].strip() != match[3]:
            end += 1
        if end == len(lines):
            i += 1
            continue
        found.append(("".join(lines[i + 1:end]), i + 1, bool(match[2]), lines[i][:match.start()]))
        for at in range(i + 1, end + 1):
            masked[at] = re.sub(r"[^\n]", " ", lines[at])
        i = end + 1
    return "".join(masked), found


def command_substitutions(text):
    """Separate nested shell command strings before tokenizing their caller."""
    def end_of(start, backtick=False):
        i, depth, quoted = start, 1, None
        while i < len(text):
            c = text[i]
            if c == "\\" and quoted != "'":
                i += 2
                continue
            if backtick and c == "`":
                return i
            if quoted == "'":
                if c == "'":
                    quoted = None
            elif text.startswith("$(", i):
                i = end_of(i + 2)
            elif c == '"':
                quoted = None if quoted == '"' else '"'
            elif not quoted:
                if c == "'":
                    quoted = "'"
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if not depth:
                        return i
            i += 1
        raise ValueError("unterminated command substitution")

    masked, substitutions = list(text), []
    i, quoted = 0, None
    while i < len(text):
        c = text[i]
        if c == "\\" and quoted != "'":
            i += 2
            continue
        if quoted == "'":
            if c == "'":
                quoted = None
        elif c == "#" and not quoted and (i == 0 or text[i - 1].isspace()):
            finish = text.find("\n", i)
            i = len(text) if finish < 0 else finish
            continue
        elif text.startswith("$(", i) or c == "`":
            width = 1 if c == "`" else 2
            finish = end_of(i + width, backtick=width == 1)
            substitutions.append((text[i + width:finish], text.count("\n", 0, i)))
            masked[i:finish + 1] = ["\n" if v == "\n" else " " for v in text[i:finish + 1]]
            i = finish
        elif c == '"':
            quoted = None if quoted == '"' else '"'
        elif c == "'" and not quoted:
            quoted = "'"
        i += 1
    return "".join(masked), substitutions


def workflow_scripts(text):
    """Read ordinary YAML run scalars; other YAML values are not commands."""
    lines = text.splitlines(True)
    i = 0
    while i < len(lines):
        match = re.match(r"^(\s*)(?:-\s*)?run:\s*(.*?)\s*$", lines[i])
        if not match:
            i += 1
            continue
        value, at = match[2], i + 1
        if re.fullmatch(r"[|>][+-]?(?:\s+#.*)?", value):
            parent = len(match[1]) + (2 if lines[i].lstrip().startswith("- ") else 0)
            body = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > parent):
                body.append(lines[i])
                i += 1
            yield "".join(body), at + 1
            continue
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        elif value.startswith('"') and value.endswith('"'):
            value = json.loads(value)
        yield value, at
        i += 1


def python_commands(text):
    module = ast.parse(text)
    literals = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    literals[target.id] = value
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
        if name not in EXECUTORS or not node.args:
            continue
        try:
            value = literals.get(node.args[0].id) if isinstance(node.args[0], ast.Name) else ast.literal_eval(node.args[0])
        except (ValueError, TypeError):
            continue
        if isinstance(value, str):
            yield from executable_commands(value, node.lineno)
        elif isinstance(value, (tuple, list)) and all(isinstance(v, str) for v in value):
            yield list(value), node.lineno


def process_commands(text):
    # Literal command/argv values in common JS/Ruby process launch calls.
    literal = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)'''
    text = re.sub(literal + r"|//[^\n]*|/\*[\s\S]*?\*/", lambda m: re.sub(r"[^\n]", " ", m[0])
                  if m[0].startswith(("//", "/*")) else m[0], text)
    launch = re.compile(r"\b(?:exec|execSync|execFile|execFileSync|spawn|spawnSync|system)\s*\(\s*(" + literal + r")\s*(?:,\s*\[([^\]]*)\])?")
    for match in launch.finditer(text):
        def decode(value):
            return ast.literal_eval(value) if value[0] != "`" else value[1:-1]
        command = decode(match[1])
        at = text.count("\n", 0, match.start()) + 1
        if match[2] is None:
            yield from executable_commands(command, at)
        else:
            args = re.findall(literal, match[2])
            yield [command, *(decode(arg) for arg in args)], at


def shell_wrappers(sources):
    """Resolve ordinary shell functions that forward argv to Git or a wrapper.

    Only literal call templates are followed. No repository code is sourced.
    """
    definitions = []
    multiline = re.compile(r"(?m)^([ \t]*)(?:function\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*\(\)\s*\{([^\n]*\n[\s\S]*?)^\1\}")
    inline = re.compile(r"(?m)^[ \t]*(?:function\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*\(\)\s*\{([^\n]*)\}")
    for text in sources:
        definitions += [(m[2], m[3]) for m in multiline.finditer(text)]
        definitions += [(m[1], m[2]) for m in inline.finditer(text)]
    templates = {}
    changed = True
    while changed:
        changed = False
        for name, body in definitions:
            if name in templates or not re.search(r"\$[@*]", body):
                continue
            try:
                commands = list(executable_commands(body))
            except ValueError:
                continue
            for argv, _ in commands:
                expanded = expand_wrapper(argv, templates)
                if expanded and expanded[0] == "git" and any(v in {"$@", "$*"} for v in expanded):
                    # A literal shift before forwarding consumes the helper's
                    # own first argument (for example its branch name).
                    shifts = sum(int(m[1] or 1) for m in re.finditer(r"(?m)^\s*shift(?:\s+([0-9]+))?\s*(?:;|$)", body))
                    templates[name] = (expanded, shifts)
                    changed = True
                    break
    return templates


def expand_wrapper(argv, templates):
    if not argv or argv[0] not in templates:
        return argv
    template, shifts = templates[argv[0]]
    args = argv[1 + shifts:]
    expanded = []
    for value in template:
        if value in {"$@", "$*"}:
            expanded.extend(args)
        else:
            expanded.append(value)
    return expanded


def unsafe_ref_update(argv):
    if (len(argv) >= 3 and argv[:2] == ["gh", "api"] and "PATCH" in argv
            and any("/git/refs/heads/" in arg for arg in argv)
            and any(arg in {"force=true", "-Fforce=true", "--field=force=true"} for arg in argv)):
        return "forced GitHub branch-ref update"
    return None


def scan(tree):
    findings, unreadable = [], []
    files = {}
    for path in sorted(tree.paths()):
        p = pathlib.PurePosixPath(path)
        if set(p.parts) & SKIP:
            continue
        shell = p.suffix in SHELL or p.name in {"Makefile", "justfile", "Dockerfile"}
        workflow = p.suffix in {".yml", ".yaml"}
        package = p.name == "package.json"
        if not (shell or workflow or package or p.suffix in SOURCE):
            continue
        text = tree.read(path)
        if text is None:
            unreadable.append({"path": path, "reason": "Git blob unavailable"})
            continue
        if p.suffix in SOURCE and not (set(p.parts[:-1]) & {"scripts", "script", "bin", ".github"}
                or text.startswith("#!") or (p.suffix == ".py" and re.search(r'''if\s+__name__\s*==\s*['"]__main__['"]''', text))):
            continue  # A product library method is not repository automation.
        files[path] = (text, shell, workflow, package)
    wrappers = shell_wrappers([text for text, shell, workflow, _ in files.values() if shell or workflow])
    for path, (text, shell, workflow, package) in files.items():
        p = pathlib.PurePosixPath(path)
        # Avoid parsing unrelated files (including old language dialects).
        if "push" not in text and "/git/refs" not in text:
            continue
        try:
            if workflow:
                commands = (cmd for body, line in workflow_scripts(text) for cmd in executable_commands(body, line))
            elif package:
                data = json.loads(text)
                commands = (cmd for body in data.get("scripts", {}).values() for cmd in executable_commands(body))
            elif p.suffix == ".py":
                commands = python_commands(text)
            elif shell:
                commands = executable_commands(re.sub(r"(?m)^[ \t]*(?:RUN|@)[ \t]*", "", text))
            else:
                commands = process_commands(text)
            seen = collections.Counter()
            for argv, line in commands:
                expanded = expand_wrapper(argv, wrappers)
                reason = unsafe_push(expanded) or unsafe_ref_update(expanded)
                if not reason:
                    continue
                digest = hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest()
                seen[digest] += 1
                findings.append({"path": path, "line": line, "command_sha256": digest,
                                 "occurrence": seen[digest], "reason": reason, "contract": CONTRACT})
        except (ValueError, TypeError, SyntaxError) as error:
            unreadable.append({"path": path, "reason": "command syntax not reviewed: " + type(error).__name__})
    return {"findings": findings, "unreadable": unreadable, "limits": LIMITS}


def baseline_key(row):
    return f"force-push {quote(row['path'], safe='/._-')} {row['command_sha256']} {row['occurrence']}"


def decide(findings, head_records, base_records, base_findings, head_adopted, base_adopted, *, allow_seed=False):
    actual = {baseline_key(row) for row in findings}
    inherited = {baseline_key(row) for row in base_findings}
    allowed = base_records | (inherited if allow_seed and not base_adopted else set())
    errors = []
    if base_adopted and not head_adopted:
        errors.append("force-push ratchet marker cannot be removed")
    if head_records and not head_adopted:
        errors.append("force-push baseline records require " + BASELINE_MARKER)
    for key in sorted(head_records - allowed):
        errors.append("force-push baseline cannot grow or seed new debt: " + key)
    for key in sorted(head_records - actual):
        errors.append("stale force-push baseline record: " + key + "; remove it")
    for row in findings:
        key = baseline_key(row)
        row["baseline_recorded"] = key in head_records
        if key not in head_records & allowed:
            errors.append(f"{row['path']}:{row['line']}: force push is forbidden ({row['reason']}); use a normal push and the merge queue ({CONTRACT})")
    return errors
