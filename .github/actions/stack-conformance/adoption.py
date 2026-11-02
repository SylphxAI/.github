"""Offline, rule-scoped adoption of inherited debt during a shared checker bump.

Old code is parsed as data, never imported. The checked-in registry binds each
published checker commit to its source hashes and enforced rule capabilities.
Only adoption needs this registry; ordinary shrinking ratchets do not.
"""
from __future__ import annotations

import argparse
import ast
import base64
import datetime
import hashlib
import importlib.util
import json
import pathlib
import re
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
REGISTRY = HERE / "checker-capabilities.json"
CACHE_APPROVALS = HERE / "cache-safe-reads.json"
CACHE_RULE = "architecture/C1/cache-economy"
PREFIX = ".github/actions/stack-conformance/"
RULE_MARKER = "# stack-rule-adopted-v1 "
STACK_USE = re.compile(
    r"^\s*-?\s*uses\s*:\s*['\"]?SylphxAI/\.github/\.github/actions/stack-conformance@([^\s'\"#]+)")


def workflow_files(tree):
    files = {}
    for path in tree.paths():
        if path.startswith((".github/workflows/", ".github/actions/")) and path.endswith((".yml", ".yaml")):
            text = tree.read(path)
            if text is None:
                raise ValueError("workflow input unreadable: " + path)
            files[path] = text
    return files


def pin_checker():
    path = HERE.parents[2] / "scripts/action_pin_drift_check.py"
    spec = importlib.util.spec_from_file_location("stack_action_pin_drift", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main_marker_errors(tree, repository):
    return [f"{row['file']}:{row['line']}: {row['why']}"
            for row in pin_checker().main_marker_errors(workflow_files(tree),
                require_main_marker=repository.lower() != "sylphxai/.github")]


def pin_provenance(tree, repository, shared_source=None):
    return pin_checker().review_pins(workflow_files(tree), repository, shared_source)


def checker_pins(tree, *, required=True):
    pins = set()
    for text in workflow_files(tree).values():
        for line in text.splitlines():
            match = STACK_USE.match(line)
            if match:
                if not re.fullmatch(r"[a-fA-F0-9]{40}", match[1]):
                    raise ValueError("old stack checker must use an immutable full SHA")
                pins.add(match[1].lower())
    if not pins and required:
        raise ValueError("no immutable old stack-conformance caller found at base")
    return sorted(pins)


def architecture_rules(source):
    """Extract literal rule identities from the recognizer's add calls."""
    tree = ast.parse(source)
    scan = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "scan_file")
    rules = set()
    for node in ast.walk(scan):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "add":
            if (len(node.args) < 3 or not all(isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                                            for arg in node.args[1:3])):
                raise ValueError("unrecognized architecture rule capability; review the source")
            cls, rule = (arg.value for arg in node.args[1:3])
            if not re.fullmatch(r"C[1-7]", cls) or not re.fullmatch(r"[a-z-]+", rule):
                raise ValueError("invalid architecture rule identity")
            rules.add("architecture/" + cls + "/" + rule)
    if not rules:
        raise ValueError("no architecture rule capabilities found")
    return rules


def source_profile(tree):
    """Reviewable capability evidence from immutable git blobs, without execution."""
    sources = {}
    texts = {}
    for name in ("action.yml", "stack_conformance.py", "architecture.py", "model_pins.py"):
        path = PREFIX + name
        value = tree.read(path)
        if value is None and path in tree.paths():
            raise ValueError("unreadable checker source: " + path)
        sources[path] = hashlib.sha256(value.encode()).hexdigest() if value is not None else None
        texts[name] = value
    # Keep historical profile identities exact when the new module is absent.
    tenant_path = PREFIX + "tenant_reads.py"
    if tenant_path in tree.paths():
        value = tree.read(tenant_path)
        if value is None:
            raise ValueError("unreadable checker source: " + tenant_path)
        ast.parse(value)
        sources[tenant_path] = hashlib.sha256(value.encode()).hexdigest()
    force_path = PREFIX + "no_force_push.py"
    if force_path in tree.paths():
        value = tree.read(force_path)
        if value is None:
            raise ValueError("unreadable checker source: " + force_path)
        ast.parse(value)
        sources[force_path] = hashlib.sha256(value.encode()).hexdigest()
    domain_path = PREFIX + "cache-economy-profiles.json"
    if domain_path in tree.paths():
        value = tree.read(domain_path)
        if value is None:
            raise ValueError("unreadable checker source: " + domain_path)
        json.loads(value)
        sources[domain_path] = hashlib.sha256(value.encode()).hexdigest()
    deal_path = PREFIX + "deal_randomness.py"
    if deal_path in tree.paths():
        value = tree.read(deal_path)
        if value is None:
            raise ValueError("unreadable checker source: " + deal_path)
        ast.parse(value)
        sources[deal_path] = hashlib.sha256(value.encode()).hexdigest()
    portable_path = PREFIX + "project_portability.py"
    if portable_path in tree.paths():
        value = tree.read(portable_path)
        if value is None:
            raise ValueError("unreadable checker source: " + portable_path)
        ast.parse(value)
        sources[portable_path] = hashlib.sha256(value.encode()).hexdigest()
    scope_path = "policy/deal-randomness-scope.json"
    if deal_path in sources:
        value = tree.read(scope_path)
        if value is None:
            raise ValueError("deal randomness scope policy missing")
        json.loads(value)
        sources[scope_path] = hashlib.sha256(value.encode()).hexdigest()
    if "source_provenance" in (texts["stack_conformance.py"] or ""):
        for path, parse in (("scripts/source_provenance.py", ast.parse),
                            ("policy/source-relocations.json", json.loads)):
            value = tree.read(path)
            if value is None:
                raise ValueError("source-relocation checker dependency missing: " + path)
            parse(value)
            sources[path] = hashlib.sha256(value.encode()).hexdigest()
    if "staged_retirement" in (texts["stack_conformance.py"] or ""):
        path = PREFIX + "staged_retirement.py"
        value = tree.read(path)
        if value is None:
            raise ValueError("staged-retirement checker dependency missing")
        ast.parse(value)
        sources[path] = hashlib.sha256(value.encode()).hexdigest()
    if texts["stack_conformance.py"] is None or "stack_conformance.py" not in (texts["action.yml"] or ""):
        raise ValueError("unrecognized stack checker entrypoint")
    module = ast.parse(texts["stack_conformance.py"])
    check = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "cmd_check")
    calls = {(node.func.value.id, node.func.attr) for node in ast.walk(check)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name)}
    rules = set()
    if ("architecture", "evaluate") in calls:
        if texts["architecture.py"] is None:
            raise ValueError("architecture checker source missing")
        rules |= architecture_rules(texts["architecture.py"])
        if "architecture/C1/deal-randomness" in rules and deal_path not in sources:
            raise ValueError("deal randomness checker source missing")
        if "architecture/C1/project-portability" in rules and portable_path not in sources:
            raise ValueError("project portability checker source missing")
        if "architecture/C2/tenant-read-functions" in rules and tenant_path not in sources:
            raise ValueError("tenant declaration checker source missing")
    if ("model_pins", "decide") in calls:
        if texts["model_pins.py"] is None:
            raise ValueError("model checker source missing")
        rules |= model_rules(texts["model_pins.py"])
    if ("no_force_push", "decide") in calls:
        if force_path not in sources:
            raise ValueError("force-push checker source missing")
        rules.add("force-push")
    return {"sources": sources, "rules": sorted(rules)}


def model_rules(source):
    """Read declared coverage without importing a historical recognizer."""
    rules = {"model-pin"}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SELECTOR_RULE" for target in node.targets):
            if not isinstance(node.value, ast.Constant) or node.value.value != "model-selector":
                raise ValueError("unrecognized model selector capability")
            rules.add(node.value.value)
    return rules


def current_rules():
    return (architecture_rules((HERE / "architecture.py").read_text())
            | model_rules((HERE / "model_pins.py").read_text()) | {"force-push", "legacy/solar2d"})


def profile_id(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_registry():
    data = json.loads(REGISTRY.read_text())
    if data.get("version") != 1 or set(data) != {"version", "commits", "profiles"}:
        raise ValueError("invalid checker capability registry")
    for sha, identity in data["commits"].items():
        if not re.fullmatch(r"[a-f0-9]{40}", sha) or identity not in data["profiles"]:
            raise ValueError("invalid checker capability provenance")
    for identity, profile in data["profiles"].items():
        if profile_id(profile) != identity:
            raise ValueError("checker capability profile hash mismatch")
    return data


class PublishedCheckerTree:
    """Read immutable public Git blobs through the existing provenance client."""

    def __init__(self, pin, gh=None):
        checker = pin_checker()
        self.gh = gh or checker.om.Gh()
        commit = checker.Commits(self.gh).commit(pin)
        tree_sha = commit["commit"]["tree"]["sha"]
        tree = self.gh.rest(f"repos/{checker.SHARED_REPO}/git/trees/{tree_sha}?recursive=1")
        if tree.get("truncated") or tree.get("sha") != tree_sha:
            raise ValueError("immutable checker tree is incomplete or mismatched")
        self.blobs = {row["path"]: row["sha"] for row in tree["tree"]
                      if row["type"] == "blob" and row.get("mode") in ("100644", "100755")}
        self.repo = checker.SHARED_REPO

    def paths(self):
        return list(self.blobs)

    def read(self, path):
        sha = self.blobs.get(path)
        if sha is None:
            return None
        blob = self.gh.rest(f"repos/{self.repo}/git/blobs/{sha}")
        if blob.get("sha") != sha or blob.get("encoding") != "base64":
            raise ValueError("immutable checker blob response mismatch")
        raw = base64.b64decode("".join(blob["content"].split()), validate=True)
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if actual != sha:
            raise ValueError("immutable checker blob content mismatch")
        return raw.decode("utf-8")


def old_checkers(base_tree, *, shared_repo=None, source_tree=None, tree_cache=None):
    registry = load_registry()
    evidence = []
    cache = {} if tree_cache is None else tree_cache
    def read_source(pin):
        if pin not in cache:
            cache[pin] = (_checker().Tree(shared_repo, pin) if shared_repo is not None
                          else (source_tree if source_tree is not None else PublishedCheckerTree)(pin))
        return cache[pin]
    for pin in checker_pins(base_tree):
        identity = registry["commits"].get(pin)
        actual = None
        if identity is None:
            unknown = "unknown old checker pin; register verified source capabilities before adoption: " + pin
            if shared_repo is None and source_tree is None:
                raise ValueError(unknown)
            try:
                actual = source_profile(read_source(pin))
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, SyntaxError,
                    StopIteration, subprocess.SubprocessError) as error:
                raise ValueError(unknown + "; checker source is unavailable or invalid: " + str(error)) from None
            identity = profile_id(actual)
            if identity not in registry["profiles"]:
                raise ValueError(unknown + "; immutable source profile has not been reviewed")
        profile = registry["profiles"][identity]
        if shared_repo is not None:
            if actual is None:
                actual = source_profile(read_source(pin))
            if actual != profile:
                raise ValueError("old checker source capability provenance mismatch: " + pin)
        evidence.append({"commit": pin, "profile": identity, **profile})
    return evidence


def adopted_rules(text):
    return {line[len(RULE_MARKER):] for line in (text or "").splitlines() if line.startswith(RULE_MARKER)}


def cache_approval_errors(findings, repository):
    """Initial cache adoption admits only reviewed, exact read occurrences."""
    data = json.loads(CACHE_APPROVALS.read_text())
    if data.get("version") != 1 or not isinstance(data.get("reads"), list):
        raise ValueError("invalid cache read approval policy")
    approved = set()
    for read in data["reads"]:
        if not isinstance(read.get("reason"), str) or not read["reason"].strip() or not read.get("bindings"):
            raise ValueError("cache read approval requires a reason and exact bindings")
        for binding in read["bindings"]:
            repo, record = binding.get("repository"), binding.get("record")
            if (not isinstance(repo, str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", repo)
                    or not isinstance(record, str)
                    or not re.fullmatch(r"architecture C1 cache-economy [^\s]+ [a-f0-9]{64} [1-9][0-9]*", record)):
                raise ValueError("invalid exact cache read approval binding")
            identity = (repo, record)
            if identity in approved:
                raise ValueError("duplicate cache read approval binding")
            approved.add(identity)
    return ["unapproved inherited cache read; fix or delete before adoption: " + row["path"]
            + ":" + str(row["line"])
            for row in findings if row["rule"] == "cache-economy" and not row.get("decision")
            and (repository, "architecture " + row["class"] + " " + row["rule"] + " "
                 + row["path"] + " " + row["span_sha256"] + " " + str(row["occurrence"])) not in approved]


def cache_adoption_requested(base_tree, head_tree, base_text, head_text):
    """A pin-only bump cannot skip initial approval by omitting its marker."""
    if CACHE_RULE in adopted_rules(head_text) - adopted_rules(base_text):
        return True
    before = checker_pins(base_tree, required=False)
    after = checker_pins(head_tree, required=False)
    # A pre-existing marker is not evidence that the prior checker enforced
    # approvals. Revalidate on pin adoption; unchanged-pin CI is unaffected.
    return bool(after) and before != after


def legacy_rules(profile, *, shared_repo=None, source_tree=None, tree_cache=None):
    """Prove category introduction from profile-bound old source, never execute it.

    Historical profile hashes and rule lists stay immutable. This additional
    conservative read treats any Solar2D literal in the old departures function
    as prior enforcement: a renamed adoption category cannot reset the guard.
    """
    cache = {} if tree_cache is None else tree_cache
    pin = profile["commit"]
    if pin not in cache:
        cache[pin] = (_checker().Tree(shared_repo, pin) if shared_repo is not None
                      else (source_tree if source_tree is not None else PublishedCheckerTree)(pin))
    tree = cache[pin]
    path = PREFIX + "stack_conformance.py"
    source = tree.read(path)
    if source is None or hashlib.sha256(source.encode()).hexdigest() != profile["sources"].get(path):
        raise ValueError("old legacy category source provenance mismatch")
    scan = next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == "departures")
    return {"legacy/solar2d"} if any(isinstance(node, ast.Constant) and isinstance(node.value, str)
           and "solar2d" in node.value.lower() for node in ast.walk(scan)) else set()


def introduced_rules(base_tree, baseline_text, *, shared_repo=None, source_tree=None, head_tree=None):
    source_cache = {}
    if head_tree is not None and not checker_pins(base_tree, required=False):
        # Initial onboarding still requires an immutable candidate caller.
        checker_pins(head_tree)
        profiles = []
    else:
        profiles = old_checkers(base_tree, shared_repo=shared_repo, source_tree=source_tree, tree_cache=source_cache)
    enforced = {rule for profile in profiles for rule in profile["rules"]}
    for profile in profiles:
        # Only category adoption needs this extra immutable-source read.
        if "legacy/solar2d" not in adopted_rules(baseline_text):
            enforced |= legacy_rules(profile, shared_repo=shared_repo, source_tree=source_tree,
                                     tree_cache=source_cache)
    adopted = adopted_rules(baseline_text)
    if "# model-alias-ratchet-v1" in (baseline_text or "").splitlines():
        adopted.add("model-pin")
    if "# force-push-ratchet-v1" in (baseline_text or "").splitlines():
        adopted.add("force-push")
    return current_rules() - enforced - adopted, profiles


def baseline_validation(head_text, base_text, architecture):
    """Keep duplicate records and marker resets visible before parsing into sets."""
    errors = []
    for label, text in (("head", head_text), ("base", base_text)):
        entries = [" ".join(line.split("#", 1)[0].split()) for line in (text or "").splitlines()]
        seen = set()
        for entry in filter(None, entries):
            if entry in seen:
                errors.append("duplicate baseline record in " + label + ": " + entry)
            seen.add(entry)
            if entry.startswith("architecture "):
                parts = entry.split()
                if len(parts) != 6 or "architecture/" + parts[1] + "/" + parts[2] not in adopted_rules(text):
                    errors.append("architecture baseline record requires its rule adoption marker: " + entry)
        if architecture.LEGACY_BASELINE_MARKER in (text or "").splitlines():
            errors.append("legacy architecture baseline v1 requires explicit reviewed migration to v2")
    for rule in sorted(adopted_rules(base_text) - adopted_rules(head_text)):
        errors.append("rule adoption marker cannot be removed: " + rule)
    for rule in sorted(adopted_rules(head_text) - current_rules()):
        errors.append("unknown rule adoption marker: " + rule)
    return errors


def seed_permissions(base_tree, head_text, base_text, grown_records, *, shared_repo=None, source_tree=None,
                     head_tree=None):
    requested = adopted_rules(head_text) - adopted_rules(base_text)
    if not grown_records and not requested:
        return set()
    introduced, _ = introduced_rules(base_tree, base_text, shared_repo=shared_repo, source_tree=source_tree,
                                     head_tree=head_tree)
    if requested - introduced:
        raise ValueError("cannot adopt rules already enforced by an old checker: " + ", ".join(sorted(requested - introduced)))
    return introduced


def _checker():
    spec = importlib.util.spec_from_file_location("adoption_stack_checker", HERE / "stack_conformance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan_adoption(base_tree, head_tree, repository, *, shared_repo, today, baseline=".github/stack-departures.txt"):
    """Return baseline text, introduced rules and evidence; never mutate either tree."""
    sc = _checker()
    original = base_tree.read(baseline)
    if head_tree.read(baseline) != original:
        raise ValueError("candidate baseline differs from base; preserve original bytes before adoption")
    errors = baseline_validation(original, original, sc.architecture)
    if errors:
        raise ValueError("; ".join(errors))
    introduced, profiles = introduced_rules(base_tree, original, shared_repo=shared_repo, head_tree=head_tree)
    base_arch = sc.architecture.evaluate(base_tree, repository, None, today)
    head_arch = sc.architecture.evaluate(head_tree, repository, None, today)
    base_models = sc.model_pins.scan(base_tree, repository, None, sc.architecture.source_views)
    head_models = sc.model_pins.scan(head_tree, repository, None, sc.architecture.source_views)
    base_force = sc.no_force_push.scan(base_tree)
    head_force = sc.no_force_push.scan(head_tree)
    if any(report["unreadable"] for report in (base_arch, head_arch, base_models, head_models, base_force, head_force)):
        raise ValueError("adoption source unreadable; no baseline generated")
    if CACHE_RULE in introduced:
        errors = cache_approval_errors(head_arch["findings"], repository)
        if errors:
            raise ValueError("; ".join(errors))
    additions = set()
    if not profiles and original is None:
        inherited_departures = set(sc.departures(base_tree))
        candidate_departures = set(sc.departures(head_tree))
        if candidate_departures - inherited_departures:
            raise ValueError("new head stack departure cannot be adopted: "
                             + ", ".join(sorted(candidate_departures - inherited_departures)))
        additions |= {line for line in candidate_departures if not line.startswith("solar2d ")}
    for before, after, key, rule_id in (
            (base_arch, head_arch, sc.architecture.baseline_key,
             lambda row: "architecture/" + row["class"] + "/" + row["rule"]),
            (base_models, head_models, sc.model_pins.baseline_key, sc.model_pins.rule_id),
            (base_force, head_force, sc.no_force_push.baseline_key, lambda row: "force-push")):
        inherited = {key(row) for row in before["findings"] if not row.get("scope_warning")}
        for row in after["findings"]:
            if row.get("scope_warning"):
                continue
            if rule_id(row) in introduced:
                if key(row) not in inherited:
                    raise ValueError("new head debt cannot be adopted: " + key(row))
                if not row.get("decision"):
                    additions.add(key(row))
    retirement_text, retirement_additions = sc.staged_retirement.plan(
        base_tree, head_tree, original, introduced, sc.departures, sc.parse_baseline, baseline)
    additions |= retirement_additions
    existing = sc.parse_baseline(original) or set()
    additions -= existing
    lines = []
    if any(rule.startswith("architecture/") for rule in introduced) and sc.architecture.BASELINE_MARKER not in (original or "").splitlines():
        lines.append(sc.architecture.BASELINE_MARKER)
    if introduced & {"model-pin", "model-selector"} and sc.model_pins.BASELINE_MARKER not in (original or "").splitlines():
        lines.append(sc.model_pins.BASELINE_MARKER)
    if "force-push" in introduced and sc.no_force_push.BASELINE_MARKER not in (original or "").splitlines():
        lines.append(sc.no_force_push.BASELINE_MARKER)
    lines += [RULE_MARKER + rule for rule in sorted(introduced)]
    lines += sorted(additions)
    text = retirement_text
    if lines:
        text += ("\n" if text and not text.endswith("\n") else "") + "\n".join(lines) + "\n"
    return {"baseline_text": text, "introduced_rules": sorted(introduced),
            "evidence": {"old_checkers": profiles, "added_records": sorted(additions)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    adopt = commands.add_parser("adopt")
    adopt.add_argument("--repo", required=True)
    adopt.add_argument("--base", required=True)
    adopt.add_argument("--head", required=True, help="immutable commit or tree OID, e.g. git write-tree")
    adopt.add_argument("--shared-repo", required=True, help="local shared repository with old checker git objects")
    adopt.add_argument("--repository", required=True)
    adopt.add_argument("--baseline", default=".github/stack-departures.txt")
    adopt.add_argument("--output", required=True)
    adopt.add_argument("--evidence", required=True)
    adopt.add_argument("--today", default="")
    register = commands.add_parser("register", help="emit a source profile for review into the bundled registry")
    register.add_argument("--shared-repo", required=True)
    register.add_argument("--rev", required=True)
    args = parser.parse_args(argv)
    sc = _checker()
    try:
        if args.command == "register":
            sha = sc.git(args.shared_repo, "rev-parse", "--verify", args.rev + "^{commit}").strip()
            profile = source_profile(sc.Tree(args.shared_repo, sha))
            print(json.dumps({"commit": sha, "profile": profile_id(profile), **profile}, indent=2))
            return 0
        base = sc.git(args.repo, "rev-parse", "--verify", args.base + "^{commit}").strip()
        head = sc.git(args.repo, "rev-parse", "--verify", args.head + "^{tree}").strip()
        today = datetime.date.fromisoformat(args.today) if args.today else datetime.datetime.now(datetime.timezone.utc).date()
        result = plan_adoption(sc.Tree(args.repo, base), sc.Tree(args.repo, head), args.repository,
                               shared_repo=args.shared_repo, today=today, baseline=args.baseline)
        evidence = {"repository": args.repository, "base_commit": base, "head_tree": head,
                    "introduced_rules": result["introduced_rules"], **result["evidence"]}
        pathlib.Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.output).write_bytes(result["baseline_text"].encode())
        pathlib.Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"adopted {len(result['introduced_rules'])} newly introduced rules; added {len(evidence['added_records'])} inherited records")
        return 0
    except (ValueError, OSError, StopIteration, SyntaxError, subprocess.CalledProcessError) as error:
        parser.exit(1, "adoption refused: " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
