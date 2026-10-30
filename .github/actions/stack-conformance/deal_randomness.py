"""Recognize insecure randomness reachable from card, tile and dice operations.

This is a structural regression check, not a proof of entropy. It follows named
helpers within a file and explicit generator bindings; unknown cross-file data
flow remains part of the source inventory. Cosmetic functions are outside scope.
"""
from __future__ import annotations

import pathlib
import posixpath
import re
import tomllib
import weakref
import hashlib
import json

SOURCE = {".rs", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".java", ".lua", ".cs"}
IDENT = r"[A-Za-z_$][\w$]*"
DOMAIN = re.compile(r"(?:^|[^a-z])(?:cards?|deck\w*|tiles?|mahjong\w*|dice|walls?|hands?)(?:$|[^a-z])", re.I)
COSMETIC = re.compile(r"particle|animation|animate|render|shader|confetti|sparkle|yaw|camera|randomForce", re.I)
RANDOM = re.compile(r"Math\s*\.\s*random\b|math\s*\.\s*random(?:seed)?\b|"
                    r"\bnew\s+(?:java\s*\.\s*util\s*\.\s*)?Random\s*\(|"
                    r"\b(?:UnityEngine\s*\.\s*)?Random\s*\.\s*(?:Range|value|InitState)\b")
SEED = re.compile(r"\b(?:StdRng|SmallRng|ChaCha(?:8|12|20)Rng)\s*::\s*(?:seed_from_u64|from_seed)\s*\(")
BAD_TIME = re.compile(r"\b(?:SystemTime|UNIX_EPOCH|timestamp\w*|elapsed|as_millis|as_nanos|DateTime|Ticks)\b|\b(?:time|now)\s*\(", re.I)
CUSTOM = re.compile(r"\b(?:SmallRng|XorShift\w*|Xoshiro\w*|Pcg\w*|SplitMix\w*|JavaRandom|DeterministicRandomizer)\b", re.I)
STATE_STEP = re.compile(r"\^=\s*[^;\n]*(?:<<|>>)|(?:seed|state)\s*=\s*[^;\n]*wrapping_mul|"
                        r"\b\w+\s*=\s*\([^;\n]*\^[^;\n]*(?:<<|>>)[^;\n]*wrapping_mul")
SKIP = {"tests", "test", "fixtures", "__fixtures__", "__tests__", "examples", "docs", "ci", ".github", "oracle", "benches",
        "analysis", "verify", "experimental",
        "node_modules", "thirdparty", "third_party", "third-party", "external",
        "target", "dist", "build", ".next"}
_CARGO = weakref.WeakKeyDictionary()
SCOPE_POLICY = pathlib.Path(__file__).resolve().parents[3] / "policy/deal-randomness-scope.json"


def excluded(path):
    p = pathlib.PurePosixPath(path)
    # A checked-in runtime engine can live under vendor; its deal source is
    # still checked. Installed third-party packages and test fixtures are not.
    return (bool({part.casefold() for part in p.parts} & SKIP) or p.name.startswith("test_")
            or bool(re.search(r"(?:[._-](?:test|spec)|_test)\.", p.name)))


def words(value):
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()


def domain(value):
    return bool(DOMAIN.search(words(value)))


def analytical(name):
    token = words(name)
    return bool(re.search(r"(?:^|_)(?:test\w*|\w*test|simulat\w*|measure\w*|benchmark\w*|bench)(?:_|$)", token))


def operation(name, signature, body):
    token = words(name)
    if analytical(name):
        return False
    if COSMETIC.search(name) and not domain(name):
        return False
    # A commercial offer is also called a deal. Array shuffles and puzzle
    # generation become relevant through a card/tile/dice caller, rather than
    # treating every general-purpose list helper as an outcome operation.
    if re.search(r"iap|sale|offer|tick|rest_days|time_passed", token) and not domain(name):
        return False
    if re.match(r"(?:pre_)?deal(?:_|$)", token) or token == "deal":
        return True
    if re.search(r"(?:^|_)(?:shuffle\w*|deal\w*|roll_dice|roll_dices)(?:_|$)", token):
        return domain(signature + body)
    if domain(name) and re.search(r"(?:^|_)(?:make\w*|build\w*|creat\w*|generat\w*)(?:_|$)", token):
        return True
    if "draw" in token and domain(signature + body):
        # Canvas draw calls and pose selection are not game outcome draws.
        return not (COSMETIC.search(name) or re.search(r"\b(?:GL|Graphics|Gizmos)\s*\.|\b(?:RenderTexture|Material)\b", signature + body))
    if re.search(r"(?:^|_)(?:start|new)_?(?:hand|round)(?:_|$)", token):
        return domain(signature + body)
    if re.search(r"(?:rng|random|roll|draw|pick|generat)\w*", token) and re.search(r"\b\w*(?:randSymb|reelsData|reward|loot|prize|payout)\w*\b", signature + body, re.I):
        return True
    return False


def cargo_scope(tree):
    """Conservative path-dependency graph; dev-only oracle code is excluded."""
    if tree in _CARGO:
        return _CARGO[tree]
    manifests = {}
    for path in tree.paths():
        if pathlib.PurePosixPath(path).name != "Cargo.toml" or excluded(path):
            continue
        text = tree.read(path)
        if text is None:
            return None
        try:
            manifests[path] = tomllib.loads(text)
        except ValueError:
            return None
    packages = {path: data for path, data in manifests.items() if "package" in data}
    roots = {path for path in packages if "vendor" not in pathlib.PurePosixPath(path).parts}
    edges = {path: set() for path in packages}
    patches = {}
    for patch_path, data in manifests.items():
        for section in data.get("patch", {}).values():
            if isinstance(section, dict):
                patches.update({name: (dependency, posixpath.dirname(patch_path))
                                for name, dependency in section.items()})
    for path, data in packages.items():
        parent = posixpath.dirname(path)
        ancestors = [p for p, d in manifests.items() if "workspace" in d
                     and (not posixpath.dirname(p) or path.startswith(posixpath.dirname(p) + "/"))]
        workspace = max(ancestors, key=len) if ancestors else None
        groups = [data.get("dependencies", {}), data.get("build-dependencies", {})]
        for target in data.get("target", {}).values():
            groups += [target.get("dependencies", {}), target.get("build-dependencies", {})]
        for group in groups:
            for name, dependency in group.items():
                base = parent
                if isinstance(dependency, dict) and dependency.get("workspace") and workspace:
                    dependency = manifests[workspace]["workspace"].get("dependencies", {}).get(name, {})
                    base = posixpath.dirname(workspace)
                patched = patches.get(dependency.get("package", name) if isinstance(dependency, dict) else name)
                if patched and (not isinstance(dependency, dict) or "path" not in dependency):
                    dependency, base = patched
                if isinstance(dependency, dict) and isinstance(dependency.get("path"), str):
                    target = posixpath.normpath(posixpath.join(base, dependency["path"], "Cargo.toml"))
                    if target in packages:
                        edges[path].add(target)
    reached = set(roots)
    pending = list(roots)
    while pending:
        for target in edges[pending.pop()] - reached:
            reached.add(target)
            pending.append(target)
    result = (packages, reached)
    _CARGO[tree] = result
    return result


def runtime_source(path, tree):
    if tree is None or "vendor" not in pathlib.PurePosixPath(path).parts or not path.endswith(".rs"):
        return True
    scope = cargo_scope(tree)
    if scope is None:
        return True  # Unknown graphs are not silently certified outside scope.
    packages, reached = scope
    ancestors = [p for p in packages if path.startswith(posixpath.dirname(p) + "/")]
    return not ancestors or max(ancestors, key=len) in reached


def lua_views(text):
    clean, code = list(text), list(text)
    token = re.compile(r"--\[(=*)\[[\s\S]*?\]\1\]|--[^\n]*|\[(=*)\[[\s\S]*?\]\2\]|"
                       r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", re.S)
    for match in token.finditer(text):
        masked = re.sub(r"[^\n]", " ", match.group())
        code[match.start():match.end()] = masked
        if match.group().startswith("--"):
            clean[match.start():match.end()] = masked
    return "".join(clean), "".join(code), []


def lua_bodies(code):
    pattern = re.compile(r"\bfunction\s+(?P<name>[\w.:]+)\s*\([^)]*\)|"
                         r"\b(?P<assigned>\w+)\s*=\s*function\s*\([^)]*\)")
    for match in pattern.finditer(code):
        depth, pending_do, end = 1, 0, len(code)
        for block in re.finditer(r"\b(?:function|if|for|while|repeat|do|end|until)\b", code[match.end():]):
            token = block.group()
            if token in {"for", "while"}:
                pending_do += 1
                depth += 1
            elif token == "do" and pending_do:
                pending_do -= 1
            elif token in {"function", "if", "repeat", "do"}:
                depth += 1
            elif token in {"end", "until"}:
                depth -= 1
            if depth == 0:
                end = match.end() + block.end()
                break
        yield match["name"] or match["assigned"], match.start(), match.end(), end


def dead_lua_branches(code):
    """Mask only a syntactically false branch, retaining all source offsets."""
    result = list(code)
    for match in re.finditer(r"\bif\s+(?P<condition>[^\n]*?)\s+then\b", code):
        condition = match["condition"].strip()
        if not re.match(r"^\(*\s*false\b", condition) or re.search(r"\bor\b|\bfunction\b", condition):
            continue
        if not re.fullmatch(r"[()\s]*false[()\s]*", condition) and not re.match(r"^\(*\s*false\s+and\b", condition):
            continue
        depth, pending_do = 1, 0
        for block in re.finditer(r"\b(?:function|if|for|while|repeat|do|else|elseif|end|until)\b", code[match.end():]):
            token = block.group()
            if token in {"else", "elseif"} and depth == 1:
                finish = match.end() + block.start()
                break
            if token in {"for", "while"}:
                depth += 1
                pending_do += 1
            elif token == "do" and pending_do:
                pending_do -= 1
            elif token in {"function", "if", "repeat", "do"}:
                depth += 1
            elif token in {"end", "until"}:
                depth -= 1
            if depth == 0:
                finish = match.end() + block.start()
                break
        else:
            continue
        result[match.end():finish] = re.sub(r"[^\n]", " ", code[match.end():finish])
    return "".join(result)


def class_scopes(code, matching_end):
    scopes = []
    pattern = r"\b(?:class|impl)\s+(\w+)[^;{}]*\{"
    for match in re.finditer(pattern, code):
        scopes.append((match[1], match.start(), matching_end(code, match.end(), "{", "}")))
    return scopes


def executable_body(code, bodies, header, start, end):
    result = list(code[header:end])
    for _, nested, _, finish in bodies:
        if start <= nested < finish <= end and nested != header:
            result[nested - header:finish - header] = re.sub(r"[^\n]", " ", code[nested:finish])
    return "".join(result)


def helper_bodies(bodies, active, code, scopes):
    def owner(at):
        owners = [scope for scope in scopes if scope[1] <= at < scope[2]]
        return min(owners, key=lambda scope: scope[2] - scope[1])[0] if owners else None
    added = set()
    receivers = {variable: kind for kind, variable in re.findall(r"\b(?:new\s+)?([A-Z]\w*)\s+(\w+)\s*(?=[;=])", code)}
    receivers.update({variable: kind for variable, kind in re.findall(r"\b(?:const|let|var)\s+(\w+)\s*=\s*new\s+(\w+)\s*\(", code)})
    for _, header, start, end in bodies:
        if header not in active:
            continue
        body = executable_body(code, bodies, header, start, end)[start - header:]
        callbacks = set(re.findall(r"\b(?:addEventListener|on|once|subscribe)\s*\([^;\n]*,\s*(\w+)\s*\)", body))
        added.update(b[1] for b in bodies if b[0] in callbacks or
                     b[0] in {"onStart", "onComplete"} and start <= b[1] < b[3] <= end)
        for match in re.finditer(r"\b(?:(" + IDENT + r")\s*(?:\.|::|:)\s*)?(" + IDENT + r")\s*\(", body):
            receiver, name = match.groups()
            caller = owner(header)
            target = caller if receiver in {None, "this", "self", "Self"} else receivers.get(receiver, receiver)
            for candidate in bodies:
                if candidate[0].split(":")[-1].split(".")[-1] != name:
                    continue
                if COSMETIC.search(candidate[0]):
                    continue
                if not scopes and receiver and receiver not in {"self", "this"}:
                    prefix = re.split(r"[.:]", candidate[0])[0]
                    if prefix != receiver:
                        continue
                if scopes and owner(candidate[1]) not in {target, None if receiver is None else target}:
                    continue
                added.add(candidate[1])
    return added


def scope_reason(repository, path, text, tree, policy_path=None):
    """Pinned, exact audit evidence can downgrade a finding to a warning.

    Evidence binds the source plus its caller/configuration population. A new
    caller or changed selection input invalidates the old purpose assertion.
    Caller-controlled configuration cannot add exceptions.
    """
    policy = json.loads(pathlib.Path(policy_path or SCOPE_POLICY).read_text())
    if policy.get("version") != 1 or not isinstance(policy.get("warnings"), list):
        raise ValueError("invalid deal randomness scope policy")
    digest = lambda value: hashlib.sha256(value.replace("\r\n", "\n").encode(errors="surrogateescape")).hexdigest()
    for entry in policy["warnings"]:
        if entry["repository"] != repository or entry["path"] != path:
            continue
        if entry["source_sha256"] != digest(text) or tree is None:
            continue
        evidence = entry["evidence"]
        if any((value := tree.read(name)) is None or digest(value) != expected for name, expected in evidence.items()):
            continue
        pattern = re.compile(entry["caller_pattern"])
        callers = {}
        for name in tree.paths():
            if pathlib.PurePosixPath(name).suffix.lower() not in SOURCE or excluded(name):
                continue
            value = tree.read(name)
            if value is not None and pattern.search(value):
                callers[name] = digest(value)
        if callers == entry["callers"]:
            return entry["reason"]
    return None


def dead_braced_branches(code, matching_end):
    result = list(code)
    for match in re.finditer(r"\bif\s*\(\s*false\s*\)\s*\{", code):
        end = matching_end(code, match.end(), "{", "}")
        result[match.end():end - 1] = re.sub(r"[^\n]", " ", code[match.end():end - 1])
    return "".join(result)


def bindings(code, matching_end, inherited_origins=None):
    """Return explicit non-cryptographic RNG bindings, plus unsafe seed origins."""
    bad, origins = {}, dict(inherited_origins or {})
    assignment = re.compile(r"\b(?P<name>" + IDENT + r")\s*(?::[^=;{}()\n]+)?=(?!=|>)\s*(?P<value>[^;\n]+)")
    for match in assignment.finditer(code):
        value = match["value"]
        if BAD_TIME.search(value) or re.fullmatch(r"\s*[\dxa-fA-F_]+(?:u\d+)?\s*", value):
            origins[match["name"]] = value
        if RANDOM.search(value) or CUSTOM.search(value):
            bad[match["name"]] = "non-cryptographic random generator"
    for match in re.finditer(r"\bconst\s+(\w+)\s*:[^={}]+\s*=", code):
        origins[match[1]] = "constant declaration"
    for match in SEED.finditer(code):
        end = matching_end(code, match.end(), "(", ")")
        seed = code[match.end():end - 1].strip()
        unsafe = ("SmallRng" in match.group() or BAD_TIME.search(seed)
                  or seed in origins or re.match(r"^(?:\d|\[\s*\d)", seed))
        if unsafe:
            prefix = code[max(0, code.rfind(";", 0, match.start()) + 1):match.start()]
            target = re.search(r"\b(" + IDENT + r")\s*(?::[^=;{}()\n]+)?=\s*$", prefix)
            if target:
                bad[target[1]] = "generator seeded from time or a constant"
    return bad, origins


def custom_types(path, code, views, tree):
    """Resolve explicit Rust RNG imports to a witnessed non-crypto state step."""
    result = set()
    if STATE_STEP.search(code):
        result.update(re.findall(r"\b(?:struct|impl)\s+(\w*(?:Rng|Random)\w*)\b", code))
    if tree is None:
        return result
    current = pathlib.PurePosixPath(path)
    for match in re.finditer(r"\buse\s+((?:crate|super|self)(?:::\w+)+)\s*;", code):
        parts = match[1].split("::")
        if not re.search(r"rng|random", "::".join(parts), re.I):
            continue
        kind = parts.pop()
        if parts[0] == "crate":
            if "/src/" not in path:
                continue
            base = pathlib.PurePosixPath(path.split("/src/", 1)[0]) / "src"
            parts = parts[1:]
        else:
            base = current.parent
            while parts and parts[0] in {"super", "self"}:
                if parts.pop(0) == "super":
                    base = base.parent
        module = base.joinpath(*parts)
        for candidate in (str(module) + ".rs", str(module / "mod.rs")):
            source = tree.read(candidate)
            if source is not None:
                if STATE_STEP.search(views(source, ".rs")[1]):
                    result.add(kind)
                break
    return result


def scan(path, text, views, named_bodies, matching_end, *, tree=None):
    suffix = pathlib.PurePosixPath(path).suffix.lower()
    if suffix not in SOURCE or excluded(path) or not runtime_source(path, tree):
        return []
    clean, code, _ = lua_views(text) if suffix == ".lua" else views(text, suffix)
    if suffix == ".lua":
        code = dead_lua_branches(code)
    else:
        code = dead_braced_branches(code, matching_end)
    # Cheap prefilter avoids parsing every animation and generated asset.
    if not (RANDOM.search(code) or SEED.search(code) or CUSTOM.search(code)
            or "Collections" in code or "keel_random" in code or "^=" in code
            or "wrapping_mul" in code or re.search(r"\buse\s+[^;]*(?:rng|Rng|Random)", code)):
        return []
    declarations = (re.sub(r"\b(?:internal|virtual|override|sealed|unsafe|partial)\b",
                           lambda match: " " * len(match.group()), code) if suffix == ".cs" else code)
    bodies = list(lua_bodies(code) if suffix == ".lua" else named_bodies(clean, declarations, ".java" if suffix == ".cs" else suffix))
    scopes = class_scopes(code, matching_end)
    if suffix in {".java", ".cs", ".ts", ".tsx", ".js", ".mjs", ".cjs"}:
        method = r"(?:^|[{};])\s*(?:(?:public|private|protected|static|final|async|internal)\s+)*(?:[\w$.<>?,\[\]]+\s+)?(?P<name>" + IDENT + r")\s*\("
        for match in re.finditer(method, declarations):
            name = match["name"]
            if name in {"if", "for", "while", "switch", "case", "catch", "function", "else", "return", "new", "using", "lock"}:
                continue
            params_end = matching_end(code, match.end(), "(", ")")
            tail = re.match(r"\s*(?::[^;{}=]+)?\s*\{", code[params_end:])
            if tail is None or not any(a <= match.start("name") < b for _, a, b in scopes):
                continue
            start = params_end + tail.end()
            end = matching_end(code, start, "{", "}")
            if not any(b[0] == name and b[2] == start for b in bodies):
                bodies.append((name, match.start("name"), start, end))
    if suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs"}:
        for match in re.finditer(r"\b(?:const|let|var)\s+(" + IDENT + r")\s*=\s*(?:async\s*)?\([^;{}]*?\)\s*(?::[^=;{}]+)?=>\s*(?!\s*\{)", code):
            depth, finish = 0, match.end()
            for finish in range(match.end(), len(code)):
                char = code[finish]
                if depth == 0 and char in ";\n":
                    break
                depth += (char in "([{") - (char in ")]}")
            bodies.append((match[1], match.start(), match.end(), finish))
    roots = [b for b in bodies if operation(b[0], code[b[1]:b[2]], code[b[2]:b[3]])]
    # Follow the RHS of direct outcome assignments, not all randomness in a
    # coordinator that also performs AI timing or chooses identifiers.
    for match in re.finditer(r"\b\w*(?:card|deck|tile|mahjong)\w*\s*(?:\[[^\]\n]*\])?\s*=(?!=)[^;\n]*", code, re.I):
        if re.search(r"\b(?:\w*random\w*|Random\s*\.\s*Range)\s*\(", match.group(), re.I):
            owners = [b for b in bodies if b[1] <= match.start() < b[3]]
            if any(analytical(b[0]) or COSMETIC.search(b[0]) for b in owners):
                continue
            sink = ("outcome_assignment", match.start(), match.start(), match.end())
            bodies.append(sink)
            roots.append(sink)
    active = {b[1] for b in roots}
    # Follow same-file helper calls without treating neighbouring cosmetics as
    # part of the deal. Each helper is visited once, even in recursive code.
    while True:
        added = helper_bodies(bodies, active, code, scopes)
        if added <= active:
            break
        active |= added
    if not active:
        return []
    global_code = list(code)
    for _, header, _, end in bodies:
        global_code[header:end] = re.sub(r"[^\n]", " ", code[header:end])
    global_code = "".join(global_code)
    global_bad, global_origins = bindings(global_code, matching_end)
    types = custom_types(path, code, views, tree) if suffix == ".rs" else set()
    if types and STATE_STEP.search(code):
        global_bad["self"] = "a non-cryptographic state generator"
    # The published Keel Rng is xoshiro, intended for visuals/simulation. Its
    # secure seed does not make its output suitable for card or tile dealing.
    if re.search(r"\buse\s+keel_random\s*::", code):
        types.add("Rng")
    # Locally implemented xorshift/LCG helpers are common legacy game sources.
    custom_body = {b[0] for b in bodies if STATE_STEP.search(code[b[2]:b[3]])
                   and re.search(r"seed|state|rng|random", code[b[1]:b[3]], re.I)
                   and re.search(r"rand|next|below|range|splitmix|shuffle", b[0], re.I)}
    rows = {}
    def add(at, end, reason):
        rows[(at, end)] = reason
    for name, header, start, end in bodies:
        if header not in active:
            continue
        body = executable_body(code, bodies, header, start, end)
        local_bad, origins = bindings(body, matching_end, global_origins)
        parameters = code[header:start]
        shadowed = set(re.findall(r"\b(" + IDENT + r")\s*(?=[:,)=])", parameters))
        shadowed.update(re.findall(r"\b(?:const|let|var)\s+(" + IDENT + r")\b", body))
        bad = {**{key:value for key,value in global_bad.items() if key not in shadowed}, **local_bad}
        for kind in types:
            for scope in (global_code, body):
                for match in re.finditer(r"\b(\w+)\s*(?::[^=;{}()\n]+)?=\s*" + re.escape(kind) + r"\s*::", scope):
                    bad[match[1]] = "a non-cryptographic state generator"
                for match in re.finditer(r"\b(\w+)\s*:\s*(?:[\w:]+::)?" + re.escape(kind) + r"\b", scope):
                    bad[match[1]] = "a non-cryptographic state generator"
        for match in RANDOM.finditer(body):
            at = header + match.start()
            owners = [b for b in bodies if b[1] <= at < b[3]]
            owner = min(owners, key=lambda b: b[3] - b[1]) if owners else None
            if owner and owner[1] not in active:
                continue
            if owner and COSMETIC.search(owner[0]):
                continue
            # Transition duration and pose noise do not choose the dice face.
            # A setFrame/math.random expression remains checked even when its
            # enclosing roll function also animates the dice.
            line = body[body.rfind("\n", 0, match.start()) + 1:match.start()]
            if suffix == ".lua" and re.search(r"\b(?:time|duration|delay)\s*=\s*$", line):
                transitions = re.finditer(r"\btransition\s*\.\s*(?:from|to)\s*\([^;{}]*\{", body)
                if any(t.end() <= match.start() < matching_end(body, t.end(), "{", "}") for t in transitions):
                    continue
            add(header + match.start(), header + match.end(), "Deal path uses " + ("math.random" if suffix == ".lua" else "a non-cryptographic random source"))
        for match in SEED.finditer(body):
            finish = matching_end(body, match.end(), "(", ")")
            seed = body[match.end():finish - 1].strip()
            if ("SmallRng" in match.group() or BAD_TIME.search(seed) or seed in origins
                    or re.match(r"^(?:\d|\[\s*\d)", seed)):
                add(header + match.start(), header + finish, "Deal generator is seeded from time or a constant")
        for match in re.finditer(r"\bCollections\s*\.\s*shuffle\s*\(", body):
            finish = matching_end(body, match.end(), "(", ")")
            if "," not in body[match.end():finish - 1]:
                add(header + match.start(), header + finish, "Collections.shuffle without SecureRandom uses java.util.Random")
        for match in re.finditer(r"\b(" + IDENT + r")\s*(?:\.|::)\s*(?:next\w*|gen\w*|random\w*|below\w*|range|pick|shuffle)\s*\(", body, re.I):
            if match[1] in bad:
                add(header + match.start(), header + match.end(), "Deal path uses " + bad[match[1]])
        for match in re.finditer(r"\b(" + IDENT + r")\s*\(", body):
            if match[1] in bad:
                add(header + match.start(), header + match.end(), "Deal path uses " + bad[match[1]])
        for match in CUSTOM.finditer(body):
            if re.match(r"\s*(?:::|\()", body[match.end():]):
                add(header + match.start(), header + match.end(), "Deal path constructs a non-cryptographic generator")
        if name in custom_body:
            add(start, end, "Deal helper implements a non-cryptographic state generator")
    # Collapse a constructor already reported as a seed violation or random
    # source, retaining distinct reads as separate shrinking-baseline records.
    result = []
    for (at, end), reason in sorted(rows.items(), key=lambda row: (row[0][0], -row[0][1])):
        if not any(left <= at and end <= right for left, right, _ in result):
            result.append((at, end, reason + "; use OS entropy with a cryptographically secure generator"))
    return result
