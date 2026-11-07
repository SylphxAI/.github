#!/usr/bin/env python3
"""Compare native name-dispatch call sites with the pinned release's JVM bytes.

No SDK source declarations or maintained export lists are used as evidence of
availability. Python and curl are required; no JVM is started.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import sys
import tarfile
import urllib.request
import urllib.parse
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import curl_retry


class ContractError(ValueError):
    pass


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, size):
        value = self.data[self.pos:self.pos + size]
        if len(value) != size:
            raise ContractError("truncated JVM class")
        self.pos += size
        return value

    def number(self, size):
        return int.from_bytes(self.take(size), "big")


class ClassFile:
    def __init__(self, data):
        r = Reader(data)
        if r.take(4) != b"\xca\xfe\xba\xbe":
            raise ContractError("not a JVM class")
        r.take(4)
        self.pool = [None] * r.number(2)
        i = 1
        while i < len(self.pool):
            tag = r.number(1)
            if tag == 1:
                value = r.take(r.number(2)).decode("utf-8", errors="replace")
            elif tag in (7, 8, 16, 19, 20):
                value = r.number(2)
            elif tag in (9, 10, 11, 12, 17, 18):
                value = (r.number(2), r.number(2))
            elif tag == 15:
                value = (r.number(1), r.number(2))
            elif tag in (3, 4, 5, 6):
                value = r.take(8 if tag in (5, 6) else 4)
            else:
                raise ContractError(f"unknown JVM constant tag {tag}")
            self.pool[i] = (tag, value)
            i += 2 if tag in (5, 6) else 1
        r.take(2)
        self.name = self.class_name(r.number(2))
        parent = r.number(2)
        self.parent = self.class_name(parent) if parent else None
        self.interfaces = [self.class_name(r.number(2)) for _ in range(r.number(2))]

        def members():
            result = []
            for _ in range(r.number(2)):
                flags, name, descriptor = r.number(2), self.utf(r.number(2)), self.utf(r.number(2))
                attrs = {}
                for _ in range(r.number(2)):
                    key = self.utf(r.number(2))
                    attrs[key] = r.take(r.number(4))
                result.append((flags, name, descriptor, attrs))
            return result

        members()  # fields
        self.methods = members()

    def utf(self, index):
        tag, value = self.pool[index]
        if tag != 1:
            raise ContractError("expected JVM UTF8 constant")
        return value

    def class_name(self, index):
        tag, value = self.pool[index]
        if tag != 7:
            raise ContractError("expected JVM class constant")
        return self.utf(value)

    def code(self, name):
        methods = [attrs["Code"] for _, n, _, attrs in self.methods if n == name and "Code" in attrs]
        if len(methods) != 1:
            raise ContractError(f"{self.name}.{name}: expected one concrete method")
        r = Reader(methods[0])
        r.take(4)
        return r.take(r.number(4))


def instructions(code):
    """Walk instructions, never mistake operand bytes for opcodes."""
    sizes = {**dict.fromkeys(range(0x15, 0x1a), 2), **dict.fromkeys(range(0x36, 0x3b), 2),
             **dict.fromkeys(range(0x99, 0xa9), 3), **dict.fromkeys(range(0xb2, 0xb9), 3),
             0x10: 2, 0x11: 3, 0x12: 2, 0x13: 3, 0x14: 3, 0x84: 3, 0xa9: 2,
             0xb9: 5, 0xba: 5, 0xbb: 3, 0xbc: 2, 0xbd: 3, 0xc0: 3, 0xc1: 3,
             0xc5: 4, 0xc6: 3, 0xc7: 3, 0xc8: 5, 0xc9: 5}
    pos = 0
    while pos < len(code):
        op = code[pos]
        size = sizes.get(op, 1)
        if op in (0xaa, 0xab):
            start = (pos + 4) & ~3
            if op == 0xaa:
                low, high = struct.unpack(">ii", code[start + 4:start + 12])
                size = start - pos + 12 + 4 * (high - low + 1)
            else:
                count = struct.unpack(">i", code[start + 4:start + 8])[0]
                size = start - pos + 8 + 8 * count
        elif op == 0xc4:
            size = 6 if code[pos + 1] == 0x84 else 4
        if size <= 0 or pos + size > len(code):
            raise ContractError("invalid JVM instruction")
        yield op, code[pos + 1:pos + size]
        pos += size


def aar_classes(data):
    with zipfile.ZipFile(io.BytesIO(data)) as aar:
        with zipfile.ZipFile(io.BytesIO(aar.read("classes.jar"))) as jar:
            return {c.name: c for name in jar.namelist() if name.endswith(".class")
                    for c in [ClassFile(jar.read(name))]}


def lua_exports(classes):
    loader = classes["plugin/cubeage/LuaLoader"]
    ops = list(instructions(loader.code("invoke")))
    allocated = [loader.class_name(int.from_bytes(arg, "big")) for op, arg in ops if op == 0xbb]
    functions = [classes[name] for name in allocated if name in classes
                 and "com/naef/jnlua/NamedJavaFunction" in classes[name].interfaces]
    # This adapter registers the constructed NamedJavaFunction array. Refuse a
    # changed mechanism rather than silently use unrelated getName constants.
    if not functions or sum(op == 0x53 for op, _ in ops) != len(functions):
        raise ContractError("unsupported LuaLoader registration table")
    method_refs = []
    for op, arg in ops:
        if op in (0xb6, 0xb9):
            tag, (owner, member) = loader.pool[int.from_bytes(arg[:2], "big")]
            _, (name, _) = loader.pool[member]
            method_refs.append((loader.class_name(owner), loader.utf(name)))
    if ("com/naef/jnlua/LuaState", "setField") not in method_refs:
        raise ContractError("LuaLoader does not register named fields")
    result = set()
    for function in functions:
        name_ops = list(instructions(function.code("getName")))
        if len(name_ops) != 2 or name_ops[0][0] not in (0x12, 0x13) or name_ops[1][0] != 0xb0:
            raise ContractError(f"nonliteral bridge name in {function.name}")
        tag, index = function.pool[int.from_bytes(name_ops[0][1], "big")]
        if tag != 8:
            raise ContractError("bridge getName does not return a string")
        result.add(function.utf(index))
    if len(result) != len(functions):
        raise ContractError("duplicate registered Lua bridge name")
    return result


def without_comments(text, language):
    # Preserve literals and offsets/line numbers while removing comments.
    pattern = (r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|--\[(=*)\[.*?\]\1\]|--[^\n]*'
               if language == "lua" else
               r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*')
    def replace(match):
        value = match.group()
        if value.startswith(("--", "//", "/*")):
            return re.sub(r"[^\n]", " ", value)
        return value
    return re.sub(pattern, replace, text, flags=re.S)


def lua_calls(text, receivers):
    text = without_comments(text, "lua")
    literals = [(m.start(), m.end()) for m in re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', text)]
    def in_literal(offset):
        return any(start <= offset < end for start, end in literals)
    receivers = set(receivers)
    # Discover direct require/pcall imports and aliases too. Adapter parameters
    # are covered by the conventional native receiver names supplied by caller.
    for m in re.finditer(r'(\w+)\s*=\s*(?:require\s*\(?|pcall\(\s*require\s*,)\s*["\']plugin\.cubeage["\']', text):
        if not in_literal(m.start()):
            receivers.add(m[1])
    changed = True
    while changed:
        changed = False
        for m in re.finditer(r'\b(\w+)\s*=\s*(\w+)\b', text):
            if m[2] in receivers and m[1] not in receivers and not in_literal(m.start()):
                receivers.add(m[1])
                changed = True
    receiver = "(?:" + "|".join(re.escape(x) for x in receivers) + ")"
    member = rf'\b{receiver}\s*(?:[.:]\s*([A-Za-z_]\w*)|\[\s*["\']([^"\']+)["\']\s*\])'
    # A contiguous return-the-first-available selector is an explicit any-of
    # contract, unlike capable()'s conjunction of required functions.
    selector = rf'if\s+type\(\s*({receiver})\.([A-Za-z_]\w*)\s*\)\s*==\s*["\']function["\']\s+then\s+return\s+\1\.\2\s+end'
    choices = [m for m in re.finditer(selector, text) if not in_literal(m.start())]
    groups = []
    for choice in choices:
        if groups and not text[groups[-1][-1].end():choice.start()].strip():
            groups[-1].append(choice)
        else:
            groups.append([choice])
    alternatives = []
    for group in groups:
        if len(group) > 1:
            alternatives.append((group[0].start(), {m[2] for m in group}))
            start, end = group[0].start(), group[-1].end()
            text = text[:start] + re.sub(r"[^\n]", " ", text[start:end]) + text[end:]
    calls = [(m.start(), {m[1] or m[2]}) for m in re.finditer(member, text) if not in_literal(m.start())]
    dynamic = next((m for m in re.finditer(rf'\b{receiver}\s*\[\s*(?!["\'\s])', text)
                    if not in_literal(m.start())), None)
    if dynamic:
        raise ContractError("dynamic Lua bridge name requires a literal dispatch")
    return sorted(calls + alternatives)


def java_methods(classes, class_name):
    result = set()
    while class_name in classes:
        c = classes[class_name]
        result.update(name for flags, name, _, _ in c.methods if flags & 1)
        class_name = c.parent
    return result


def unity_calls(text, classes):
    text = without_comments(text, "cs")
    constants = dict(re.findall(r'const\s+string\s+(\w+)\s*=\s*"([^"]+)"', text))
    receivers = {}
    for m in re.finditer(r'(\w+)\s*=\s*new\s+AndroidJava(?:Class|Object)\s*\(\s*("[^"]+"|\w+)', text):
        name = m[2].strip('"') if m[2].startswith('"') else constants.get(m[2], "")
        if name.startswith("com.cubeage."):
            receivers[m[1]] = name.replace(".", "/")
    for m in re.finditer(r'(\w+)\s*=\s*(\w+)\.GetStatic<AndroidJavaObject>\("INSTANCE"\)', text):
        if m[2] in receivers:
            receivers[m[1]] = receivers[m[2]]
    result = []
    for receiver, owner in receivers.items():
        pattern = rf'\b{re.escape(receiver)}\??\.(Call(?:Static)?)(?:<[^>]+>)?\s*\(\s*("[^"]+"|[^,)]+)'
        for m in re.finditer(pattern, text):
            if not m[2].startswith('"'):
                raise ContractError(f"dynamic Java bridge name on {receiver}")
            name = m[2].strip('"')
            result.append((m.start(), owner, name, name in java_methods(classes, owner)))
    return result


def api(path, binary=False):
    headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "native-bridge-contract"}
    token = os.environ.get("GH_TOKEN", "")
    if token:
        headers["Authorization"] = "Bearer " + token
    url = "https://api.github.com/repos/Cubeage/cubeage-native-sdk/" + path
    if binary:
        # Curl's ordinary --location policy strips Authorization on a change
        # of host, scheme or port. Never use --location-trusted for assets.
        return curl_retry.read(url, label="native-bridge-asset",
                               headers=tuple(f"{key}: {value}" for key, value in headers.items()),
                               curl_args=curl_retry.HTTP_REDIRECTS)
    request = urllib.request.Request(url, headers=headers)
    class AssetRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, url):
            redirected = super().redirect_request(req, fp, code, msg, headers, url)
            if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(req.full_url).netloc:
                redirected.remove_header("Authorization")
            return redirected
    with urllib.request.build_opener(AssetRedirect()).open(request, timeout=60) as response:
        return json.load(response)


def released_asset(ref, name):
    release = api("releases/tags/" + ref)
    if release["draft"] or release["tag_name"] != ref:
        raise ContractError("pin is not a published SDK release")
    matches = [a for a in release["assets"] if a["name"] == name]
    if len(matches) != 1:
        raise ContractError(f"{ref} has no unique released {name}")
    asset = matches[0]
    data = api("releases/assets/" + str(asset["id"]), binary=True)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if asset.get("digest") != digest:
        raise ContractError(f"{name}: release asset SHA256 differs or is absent")
    return data, digest


def check(root, artifact=None, sdk_source=None, receivers=("sdk", "plugin", "cubeageNative")):
    root = Path(root)
    settings = list(root.glob("client/**/build.settings"))
    pins = []
    for path in settings:
        match = re.search(r'cubeage-native-sdk/releases/download/(v[\d.]+)/android\.tgz', path.read_text())
        if match:
            pins.append(("lua", match[1], path.parent))
    for path in root.glob("client/**/Packages/manifest.json"):
        dependency = json.loads(path.read_text()).get("dependencies", {}).get("com.cubeage.sdk", "")
        if dependency:
            match = re.search(r'cubeage-native-sdk\.git\?path=engines/unity/com\.cubeage\.sdk#(v\d+\.\d+\.\d+)$', dependency)
            if not match:
                raise ContractError(f"{path}: SDK dependency must pin a released version")
            pins.append(("unity", match[1], path.parent.parent))
    if len(pins) != 1:
        raise ContractError(f"expected one native SDK pin, found {len(pins)}")
    engine, ref, client = pins[0]
    asset_name = "android.tgz" if engine == "lua" else f"cubeage-sdk-{ref}.aar"
    if artifact:
        data = Path(artifact).read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
    else:
        data, digest = released_asset(ref, asset_name)
    pin_file = root / "client/ci/cubeage_sdk_pin.json"
    if pin_file.exists():
        pin = json.loads(pin_file.read_text())
        if pin["ref"] != ref or pin["assets"].get(asset_name) != digest.removeprefix("sha256:"):
            raise ContractError("released artifact disagrees with the title's SDK pin digest")
    report = {"engine": engine, "pin": ref, "artifact": asset_name, "digest": digest,
              "registered": [], "calls": [], "missing": []}
    sources = []
    if engine == "lua":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = [m for m in archive.getmembers() if Path(m.name).name == "plugin-release.aar"]
            if len(members) != 1:
                raise ContractError("release has no unique Solar2D plugin AAR")
            classes = aar_classes(archive.extractfile(members[0]).read())
        exports = lua_exports(classes)
        report["registered"] = sorted(exports)
        for path in sorted(client.rglob("*.lua")):
            sources.append((str(path.relative_to(root)), path.read_text(errors="replace")))
        for path, source in sources:
            for offset, names in lua_calls(source, receivers):
                call = {"path": path, "line": source[:offset].count("\n") + 1,
                        "names": sorted(names), "available": bool(names & exports)}
                report["calls"].append(call)
    else:
        classes = aar_classes(data)
        report["registered"] = {name: sorted(java_methods(classes, name)) for name in sorted(classes)
                                if name.startswith("com/cubeage/")}
        if sdk_source:
            adapter = Path(sdk_source) / "engines/unity/com.cubeage.sdk/Runtime/Internal/AndroidBridge.cs"
            sources.append(("SDK@" + ref + "/AndroidBridge.cs", adapter.read_text()))
        else:
            # The exact pinned adapter is a caller, never the export oracle.
            adapter = api("contents/engines/unity/com.cubeage.sdk/Runtime/Internal/AndroidBridge.cs?ref=" + ref)
            import base64
            sources.append(("SDK@" + ref + "/AndroidBridge.cs", base64.b64decode(adapter["content"]).decode()))
        sources.extend((str(p.relative_to(root)), p.read_text(errors="replace"))
                       for p in sorted(client.rglob("*.cs")))
        for path, source in sources:
            for offset, owner, name, available in unity_calls(source, classes):
                report["calls"].append({"path": path, "line": source[:offset].count("\n") + 1,
                                        "class": owner, "names": [name], "available": available})
    if not report["calls"]:
        raise ContractError("no native bridge call sites found; refusing empty proof")
    report["missing"] = [c for c in report["calls"] if not c["available"]]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--artifact", help="local published asset for offline verification")
    parser.add_argument("--sdk-source", help="exact pinned SDK checkout for offline Unity adapter reads")
    parser.add_argument("--lua-receivers", default="sdk,plugin,cubeageNative")
    parser.add_argument("--report", help="write full artifact/call-site census JSON")
    args = parser.parse_args()
    try:
        report = check(args.root, args.artifact, args.sdk_source, args.lua_receivers.split(","))
        if args.report:
            Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
        for call in report["missing"]:
            print(f"::error file={call['path']},line={call['line']}::{report['pin']} does not register "
                  + " or ".join(call["names"]))
        print(f"{report['pin']} {report['artifact']} {report['digest']}: "
              f"{len(report['calls'])} references; {len(report['missing'])} missing")
        return bool(report["missing"])
    except (ContractError, KeyError, OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as error:
        print(f"::error::Native bridge contract: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
