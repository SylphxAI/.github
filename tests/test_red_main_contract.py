#!/usr/bin/env python3
"""Contract tests for the red-main handler (.github/workflows/red-main.yml).

The handler is the reusable workflow a repository calls when its after-merge
verification failed on its default branch, under the optimistic-merge
capability. Its promises are public: customers read them in Sylphx Cloud's
`docs/services/hosting/verified-commits.md`. This file holds those promises as
executable facts, so a change that quietly adds a bypass, a push to the default
branch, or an input that opts a repository in fails here rather than in a
customer's repository.

Every rule is a method on `RedMainContract`, which takes the workflow's YAML as
text. The rules therefore run twice: against the file in this repository, and
against a mutated copy of it in `AntiBackdoorTest`, which proves each rule can
fail. A failure names the file, the line, and the rule it breaks.

Nothing here reads the network or the repository's history. The one rule that
needs another repository - the public contract page - is skipped, loudly, when
no Sylphx Cloud checkout sits beside this one.
"""

from __future__ import annotations

import os
import pathlib
import re
import unittest
from typing import NoReturn

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "red-main.yml"
SOURCE = WORKFLOW.relative_to(REPO_ROOT).as_posix()
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
TESTS_SOURCE = TESTS_WORKFLOW.relative_to(REPO_ROOT).as_posix()
PRODUCT_NAMES = REPO_ROOT / ".github" / "actions" / "plain-language" / "product-names.tsv"
CONTRACT_PAGE = pathlib.PurePosixPath("docs/services/hosting/verified-commits.md")
CLOUD_CHECKOUT_ENV = "SYLPHX_CLOUD_CHECKOUT"

# The rules, in the words the accepted outcome uses. A failure prints one.
RULE_OPT_IN = (
    'opt-in only: a repository is on optimistic merge because its own sylphx.toml declares '
    '[ci] merge = "optimistic", never because a caller or an input asked for it'
)
RULE_NO_BYPASS = (
    "one public contract: the handler never uses an administrative bypass, never writes a "
    "ruleset, and never merges around the repository's own merge queue"
)
RULE_QUEUE = (
    "an automatic revert lands as a pull request through the repository's own merge queue; "
    "the only queue jump is the queue's own sanctioned enqueue"
)
RULE_GRANT = (
    "the App acts only within the installation's granted permissions, and every action it "
    "takes is recorded"
)
RULE_PRODUCTS = "platform code names no product"
RULE_DOCUMENTED = "the capability's contract is documented publicly"

READ_ONLY_PERMISSIONS = {
    "actions": "read",
    "checks": "read",
    "contents": "read",
    "pull-requests": "read",
}
# What the handler writes with, and nothing more (verified-commits.md, "What it
# never does": never uses a permission the repository did not grant).
FULL_MINT_PERMISSIONS = {
    "actions": "write",
    "contents": "write",
    "issues": "write",
    "pull-requests": "write",
}
FALLBACK_MINT_PERMISSIONS = {"actions": "write"}


class ContractViolation(AssertionError):
    """A broken rule, reported as `file:line: what broke [which rule]`."""


class RedMainContract:
    """The handler's contract, checked against the workflow's YAML text.

    One instance is one text: the committed file, or a mutated copy of it. The
    checks never look at anything else, so the same call that holds the real
    file to the contract proves a mutation of it is caught.
    """

    def __init__(self, text: str, source: str = SOURCE) -> None:
        self.text = text
        self.source = source
        self.lines = text.splitlines()

    # -- reading the workflow --------------------------------------------
    @property
    def doc(self) -> dict:
        return yaml.safe_load(self.text) or {}

    def triggers(self) -> dict:
        # PyYAML reads the `on:` key as the boolean True (YAML 1.1).
        block = self.doc.get("on", self.doc.get(True))
        if not isinstance(block, dict):
            self._fail(RULE_OPT_IN, None, "the workflow declares no `on:` triggers")
        return dict(block)

    def concurrency(self) -> dict:
        return dict(self.doc.get("concurrency") or {})

    def _call_inputs(self) -> dict:
        call = self.triggers().get("workflow_call")
        if not isinstance(call, dict):
            self._fail(RULE_OPT_IN, self._line_of(r"^\s*workflow_call:"),
                       "the handler is not a `workflow_call` reusable workflow")
        return dict(call.get("inputs") or {})

    def _job(self) -> dict:
        jobs = self.doc.get("jobs") or {}
        job = None
        if isinstance(jobs, dict):
            job = jobs.get("red-main") or next(
                (j for j in jobs.values() if isinstance(j, dict)), None)
        if not isinstance(job, dict):
            self._fail(RULE_GRANT, self._line_of(r"^jobs:"), "the handler declares no job")
        return dict(job)

    def _steps(self) -> list[dict]:
        return [s for s in (self._job().get("steps") or []) if isinstance(s, dict)]

    def _step(self, fragment: str, *, last: bool = False) -> tuple[dict, int]:
        """The step whose name contains `fragment`, or the last step."""
        steps = self._steps()
        for index, step in enumerate(steps):
            if fragment.lower() in (step.get("name") or "").lower():
                return step, index
        if last and steps:
            return steps[-1], len(steps) - 1
        self._fail(RULE_GRANT, None, f"the handler has no step named like {fragment!r}")

    # -- line numbers, for a failure that points at the line --------------
    def _lines_matching(self, pattern: str, flags: int = 0, *, code_only: bool = False) -> list[tuple[int, str]]:
        rx = re.compile(pattern, flags)
        found = [(n, line) for n, line in enumerate(self.lines, start=1) if rx.search(line)]
        if code_only:
            found = [(n, line) for n, line in found if not line.strip().startswith("#")]
        return found

    def _line_of(self, pattern: str, flags: int = 0, *, code_only: bool = False) -> int | None:
        found = self._lines_matching(pattern, flags, code_only=code_only)
        return found[0][0] if found else None

    def _step_line(self, index: int) -> int | None:
        """The line of the `- name:` that opens the step at `index`."""
        anchors = [n for n, line in enumerate(self.lines, start=1) if re.match(r"^\s*- name:", line)]
        return anchors[index] if index < len(anchors) else None

    def _fail(self, rule: str, at: int | None, detail: str) -> NoReturn:
        where = f"{self.source}:{at}" if at else self.source
        raise ContractViolation(f"{where}: {detail} [{rule}]")

    # -- the assertions ---------------------------------------------------
    def forbid(self, pattern: str, rule: str, detail: str, flags: int = 0) -> None:
        """No code line may match `pattern` (a comment may describe the rule)."""
        for n, line in self._lines_matching(pattern, flags, code_only=True):
            self._fail(rule, n, f"{detail}: {line.strip()!r}")

    def require(self, pattern: str, rule: str, detail: str, flags: int = 0, near: str = "") -> int:
        """Some code line must match `pattern`; `near` points at where it belongs."""
        found = self._lines_matching(pattern, flags, code_only=True)
        if not found:
            self._fail(rule, self._line_of(near, re.IGNORECASE, code_only=True) if near else None, detail)
        return found[0][0]

    def require_gap(self, first: str, second: str, rule: str, detail: str,
                    window: int = 400, near: str = "") -> int:
        """`second` must appear within `window` characters after `first`."""
        rx = re.compile(re.escape(first) + r"[\s\S]{0,%d}?" % window + re.escape(second))
        match = rx.search(self.text)
        if not match:
            at = self._line_of(re.escape(first))
            if at is None and near:
                at = self._line_of(near, re.IGNORECASE, code_only=True)
            self._fail(rule, at, detail)
        return match.start()

    # 1. Opt-in only, by the repository's own declaration ------------------
    def check_opt_in_only(self) -> None:
        for name in ("declared", "mode"):
            if name in self._call_inputs():
                self._fail(
                    RULE_OPT_IN,
                    self._line_of(rf"^\s+{name}\s*:"),
                    f"`{name}` is an input on the reusable workflow, so a caller could turn the "
                    "handler on or pick its mode instead of the repository declaring "
                    '[ci] merge = "optimistic" itself',
                )

        # The gate reads the caller's own committed declaration, over the API.
        self.require(
            r'gh api "repos/\$REPO/contents/sylphx\.toml"',
            RULE_OPT_IN,
            "the gate does not read sylphx.toml from the caller repository over the API",
            near=r"sylphx\.toml",
        )
        self.require(
            r"proceed=false",
            RULE_OPT_IN,
            "the gate does not default to not proceeding, so a failure to read the declaration "
            "would act",
            near=r"proceed=false|proceed=true",
        )
        self.require_gap(
            "act)", "proceed=true", RULE_OPT_IN,
            "the gate does not tie proceeding to the `act` verdict alone",
            window=160, near=r"proceed=true",
        )
        self.require_gap(
            '"off"', "no sylphx.toml", RULE_OPT_IN,
            "the gate does not name the absent sylphx.toml as the reason a repository is off "
            "(a repository with no manifest has declared nothing, so it must be a no-op)",
            window=240, near=r"sylphx\.toml",
        )
        self.require(
            r'!= "optimistic"', RULE_OPT_IN,
            "the gate does not require merge = \"optimistic\" to act, so another value could "
            "be read as opted in",
            near=r"optimistic",
        )

        # Nothing that can write runs before the declaration is read.
        steps = self._steps()
        if not any("gate" in (step.get("id") or "") for step in steps):
            self._fail(RULE_OPT_IN, None, "no step reads the declaration, so nothing could gate on it")
        for index, step in enumerate(steps):
            if "create-github-app-token" in (step.get("uses") or ""):
                condition = str(step.get("if") or "")
                if "steps.gate.outputs." not in condition:
                    self._fail(
                        RULE_OPT_IN,
                        self._step_line(index),
                        f"the step {step.get('name')!r} mints an App token without being gated on "
                        f"the declaration the gate step read (if: {condition.strip()!r})",
                    )

    # 2. One public contract, no special path, no bypass -------------------
    def check_no_bypass(self) -> None:
        self.forbid(
            r"--admin", RULE_NO_BYPASS,
            "an administrative merge bypasses the repository's ruleset and its queue",
        )
        self.forbid(
            r"\bbypass\w*", RULE_NO_BYPASS,
            "the handler names a bypass; a red main is one of the queue's own sanctioned cases, "
            "never a bypass",
            flags=re.IGNORECASE,
        )
        for n, line in self._lines_matching(r"rulesets?", code_only=True):
            if re.search(r"--method\s+(POST|PUT|PATCH|DELETE)|-X\s*(POST|PUT|PATCH|DELETE)", line):
                self._fail(
                    RULE_NO_BYPASS, n,
                    f"a ruleset write call; the handler never writes a ruleset: {line.strip()!r}",
                )

        # The queue decides the strategy, so no `gh pr merge` carries one.
        merges = self._lines_matching(r"\bgh pr merge\b", code_only=True)
        if not merges:
            self._fail(
                RULE_QUEUE, self._line_of(r"revert", re.IGNORECASE, code_only=True),
                "the handler never merges a pull request through the repository's own queue",
            )
        for n, line in merges:
            strategy = re.search(r"(?<![\w-])(--squash|--rebase|--merge|-s|-m|-r)(?![\w-])", line)
            if strategy:
                self._fail(
                    RULE_NO_BYPASS, n,
                    f"`gh pr merge` picks the merge strategy with {strategy.group(0)!r}; the "
                    f"repository's queue decides it: {line.strip()!r}",
                )

    # 3. A revert is a pull request through the repository's own queue ------
    def check_revert_lands_in_the_queue(self) -> None:
        # Never a push to the default branch, never a force on it.
        self.forbid(
            r"refs/heads/(main|master)\b", RULE_QUEUE,
            "the default branch as a ref destination; the handler never writes to it",
        )
        self.forbid(
            r"\bpush\b[^\n]*\b(main|master)\b", RULE_QUEUE,
            "a push to the default branch by name",
        )
        for n, line in self._lines_matching(r"(force=true|--force)", code_only=True):
            if re.search(r"\b(main|master)\b", line):
                self._fail(
                    RULE_QUEUE, n,
                    f"a force applied to the default branch: {line.strip()!r}",
                )

        # The revert is a branch, a pull request, and auto-merge.
        self.require(
            r'HEAD:refs/heads/\$', RULE_QUEUE,
            "the handler does not push the revert to a branch of its own; an automatic revert "
            "lands as a branch, a pull request, and auto-merge, never as a push",
            near=r"revert|branch",
        )
        self.require(
            r'\bgh pr create\b|--method POST "repos/\$REPO/pulls"', RULE_QUEUE,
            "the handler does not open the revert as a pull request (no `gh pr create` and no "
            "POST to the repository's pulls API)",
            near=r"revert",
        )
        self.require(
            r"\bgh pr merge --auto\b", RULE_QUEUE,
            "the revert pull request is not armed for the repository's own merge queue",
            near=r"revert|pulls",
        )

        # The one jump, and it is the queue's own enqueue mutation.
        jumps = self._lines_matching(r"\bjump\b\s*[:=]\s*(?:true|1)\b", flags=re.IGNORECASE, code_only=True)
        if len(jumps) > 1:
            self._fail(
                RULE_QUEUE, jumps[1][0],
                f"{len(jumps)} queue jumps in one handler; the queue's rule is one jump, and this "
                "handler spends it once",
            )
        for n, line in jumps:
            if "enqueuePullRequest" not in line:
                self._fail(
                    RULE_QUEUE, n,
                    f"a queue jump that is not the queue's own enqueue mutation: {line.strip()!r}",
                )

    # 4. Granted powers only, and every action audited ----------------------
    def check_grant_and_audit(self) -> None:
        job = self._job()
        # The job's block wins when it declares one; otherwise the workflow's.
        effective = job.get("permissions")
        where = "the handler job"
        if effective is None:
            effective, where = self.doc.get("permissions"), "the workflow"
        if effective != READ_ONLY_PERMISSIONS:
            self._fail(
                RULE_GRANT, self._line_of(r"^\s*permissions:"),
                f"{where} runs with {effective!r}; the default token stays read-only "
                f"({READ_ONLY_PERMISSIONS}) because every write goes through the App token",
            )

        # Every write is made with the minted App token, never the job token.
        for index, step in enumerate(self._steps()):
            run = str(step.get("run") or "")
            if not re.search(r"--method\s+(POST|PUT|PATCH|DELETE)", run):
                continue
            token = (step.get("env") or {}).get("GH_TOKEN") or ""
            if "app-token" not in token:
                self._fail(
                    RULE_GRANT,
                    self._step_line(index),
                    f"the step {step.get('name')!r} writes with {token or 'no token'!r}; every "
                    "write is made with the minted App token, so the repository's grant bounds it",
                )

        # The two mints, and exactly the permissions they ask for.
        mints = [(index, step) for index, step in enumerate(self._steps())
                 if "create-github-app-token" in (step.get("uses") or "")]
        if len(mints) != 2:
            self._fail(
                RULE_GRANT, self._line_of("create-github-app-token", code_only=True),
                f"the handler mints {len(mints)} App tokens; it mints one carrying the four "
                "write permissions and one carrying the observation grant alone",
            )
        asked = []
        for index, step in mints:
            with_block = dict(step.get("with") or {})
            asked.append({
                key[len("permission-"):]: value
                for key, value in with_block.items()
                if key.startswith("permission-")
            })
        if FULL_MINT_PERMISSIONS not in asked:
            self._fail(
                RULE_GRANT, self._step_line(mints[0][0]),
                f"the full mint asks for {asked[0]!r}, not exactly {FULL_MINT_PERMISSIONS}",
            )
        if FALLBACK_MINT_PERMISSIONS not in asked:
            self._fail(
                RULE_GRANT, self._step_line(mints[1][0]),
                f"the fallback mint asks for {asked[1]!r}, not exactly "
                f"{FALLBACK_MINT_PERMISSIONS} (the observation grant alone)",
            )
        if "outcome == 'failure'" not in str(mints[1][1].get("if") or ""):
            self._fail(
                RULE_GRANT, self._step_line(mints[1][0]),
                "the narrow mint does not run only when the full mint failed, so it could hand "
                "out the observation grant in place of the full one",
            )

        # The audit trail is unconditional, and it lands on the commit.
        record, index = self._step("record", last=True)
        condition = str(record.get("if") or "")
        if "always()" not in condition:
            self._fail(
                RULE_GRANT, self._step_line(index),
                f"the record step {record.get('name')!r} runs under if: {condition.strip()!r}; the "
                "audit trail is unconditional, so it carries `always()`",
            )
        run = str(record.get("run") or "")
        if not re.search(r"repos/\$REPO/commits/[^\"']*comments", run):
            self._fail(
                RULE_GRANT, self._step_line(index),
                "the record step does not post the record to the commit as a comment",
            )

    # 5. No product names in platform code ----------------------------------
    def check_no_product_names(self) -> None:
        names = product_names()
        if not names:
            self._fail(
                RULE_PRODUCTS, None,
                f"the product-name list ({PRODUCT_NAMES.relative_to(REPO_ROOT)}) is empty or "
                "missing, so nothing holds this file to it",
            )
        for pattern, why in names:
            match = re.search(pattern, self.text, re.IGNORECASE)
            if match:
                self._fail(
                    RULE_PRODUCTS,
                    self.text.count("\n", 0, match.start()) + 1,
                    f"{match.group(0)!r} {why}",
                )

    # -- all of them, for the baseline the mutations are measured against ---
    def check_all(self) -> None:
        self.check_opt_in_only()
        self.check_no_bypass()
        self.check_revert_lands_in_the_queue()
        self.check_grant_and_audit()
        self.check_no_product_names()


def product_names() -> list[tuple[str, str]]:
    """The product names the plain-language check already knows, from its own list.

    One list, one owner: this reads `.github/actions/plain-language/
    product-names.tsv` rather than keeping a second copy of the names.
    """
    if not PRODUCT_NAMES.exists():
        return []
    names = []
    for line in PRODUCT_NAMES.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pattern, _, why = line.partition("\t")
        names.append((pattern.strip(), why.strip() or "names a product"))
    return names


def phrase_word(word: str) -> str:
    """A word to compare: case and the punctuation around it do not matter."""
    return word.strip(".,;:!?()[]{}<>`'\"*|#_-").lower()


def phrase_line(page: str, phrase: str) -> int | None:
    """The line where `phrase` starts, however the page wraps and punctuates it."""
    words = [(n, phrase_word(word)) for n, line in enumerate(page.splitlines(), start=1) for word in line.split()]
    needle = [phrase_word(word) for word in phrase.split()]
    for start in range(len(words) - len(needle) + 1):
        if [word for _, word in words[start:start + len(needle)]] == needle:
            return words[start][0]
    return None


def contract_page_candidates() -> list[pathlib.Path]:
    """Where a Sylphx Cloud checkout holding the contract page may sit."""
    roots = []
    configured = os.environ.get(CLOUD_CHECKOUT_ENV, "").strip()
    if configured:
        roots.append(pathlib.Path(configured))
    roots += [REPO_ROOT.parent / "cloud", REPO_ROOT.parent.parent / "cloud"]
    return [root / CONTRACT_PAGE for root in roots]


def contract_page() -> pathlib.Path | None:
    """The contract page from a Sylphx Cloud checkout, when one is here."""
    return next((page for page in contract_page_candidates() if page.is_file()), None)


class RedMainContractTest(unittest.TestCase):
    """The committed handler, held to its contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text() if WORKFLOW.is_file() else None

    def setUp(self) -> None:
        if self.text is None:
            manifest = REPO_ROOT / "sylphx.toml"
            if manifest.is_file() and re.search(r"^\s*merge\s*=", manifest.read_text(), re.MULTILINE):
                self.fail(
                    f"{SOURCE} is missing while this repository declares its own merge "
                    "behaviour in sylphx.toml; a repository that opts into optimistic merge "
                    "calls the handler, so the handler must exist and meet this contract"
                )
            self.skipTest(
                f"{SOURCE} is not in this checkout yet; the eager contract checks and the "
                "mutation test in AntiBackdoorTest run as soon as the handler lands"
            )

    def contract(self) -> RedMainContract:
        return RedMainContract(self.text)

    def test_opt_in_only_by_the_repositorys_own_declaration(self) -> None:
        self.contract().check_opt_in_only()

    def test_one_public_contract_without_a_bypass(self) -> None:
        self.contract().check_no_bypass()

    def test_an_automatic_revert_lands_through_the_merge_queue(self) -> None:
        self.contract().check_revert_lands_in_the_queue()

    def test_the_app_acts_within_its_grant_and_every_action_is_audited(self) -> None:
        self.contract().check_grant_and_audit()

    def test_platform_code_names_no_product(self) -> None:
        self.contract().check_no_product_names()

    def test_the_contract_is_documented_publicly(self) -> None:
        """The promises this file enforces are written down where a customer reads them."""
        page = contract_page()
        if page is None:
            self.skipTest(
                "no Sylphx Cloud checkout carries the contract page, so it cannot be read; "
                f"looked in {[str(p) for p in contract_page_candidates()]}. Set "
                f"{CLOUD_CHECKOUT_ENV} to a cloud checkout to assert this one rule"
            )
        text = page.read_text()
        missing = [
            f"{phrase!r} ({why})"
            for phrase, why in (
                ('merge = "optimistic"', "the opt-in declaration the handler gates on"),
                ("on_red", "the field that picks notify or revert"),
                ("never pushes to the default branch", "the rule the revert checks enforce"),
                ("ordinary pull request", "the rule that a revert meets the same required checks"),
                ("one jump per 30 minutes", "the queue's jump rule the handler spends once"),
                ("verified", "the check run that marks a verified tip"),
            )
            if phrase_line(text, phrase) is None
        ]
        self.assertFalse(
            missing,
            f"{page}: the public contract page does not state {', '.join(missing)} "
            f"[{RULE_DOCUMENTED}]",
        )

    def test_the_handler_has_one_public_contract_and_one_reusable_trigger(self) -> None:
        """`workflow_call` only: the caller owns the trigger, and no second path exists."""
        contract = self.contract()
        triggers = set(contract.triggers())
        self.assertEqual(triggers, {"workflow_call"}, f"{SOURCE}: the handler's triggers are {triggers}")
        concurrency = contract.concurrency()
        self.assertIs(
            concurrency.get("cancel-in-progress"), False,
            f"{SOURCE}: the handler's concurrency cancels in progress; two handlers must not "
            f"race a rerun or a revert ({concurrency!r})",
        )
        group = concurrency.get("group") or ""
        self.assertIn(
            "github.repository", group,
            f"{SOURCE}: the handler's concurrency group is {group!r}; one handler runs per "
            "repository at a time, so the group names the repository",
        )


class AntiBackdoorTest(unittest.TestCase):
    """Each rule fails on a handler that breaks it, so the rules can hold a line."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text() if WORKFLOW.is_file() else None

    def setUp(self) -> None:
        if self.text is None:
            self.skipTest(f"{SOURCE} is not in this checkout yet")

    def mutate(self, old: str, new: str) -> str:
        self.assertIn(old, self.text, f"the mutation target moved, so the mutation is stale: {old!r}")
        return self.text.replace(old, new)

    def insert_entries(self, anchor: str, entries: list[str], *, last: bool = False) -> str:
        """Insert `entries` into the mapping block that `anchor` opens.

        `last` picks the last such block, which is the effective one when a
        workflow declares permissions both at the top level and on the job.
        """
        lines = self.text.splitlines(keepends=True)
        indexes = [i for i, line in enumerate(lines) if line.strip() == anchor]
        if not indexes:
            self.fail(f"the insertion anchor moved, so the mutation is stale: {anchor!r}")
        index = indexes[-1] if last else indexes[0]
        indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())] + "  "
        added = "".join(indent + entry + "\n" for entry in entries)
        return "".join(lines[: index + 1]) + added + "".join(lines[index + 1 :])

    def test_the_contract_checks_catch_a_backdoor(self) -> None:
        """Every rule fails on a handler that breaks it, or the rule holds nothing."""
        # The baseline passes, so a failure below is caused by the mutation itself.
        RedMainContract(self.text).check_all()

        mutations = (
            (
                "an administrative merge",
                RULE_NO_BYPASS,
                lambda: self.mutate("gh pr merge --auto", "gh pr merge --auto --admin"),
                lambda c: c.check_no_bypass(),
            ),
            (
                "a push to the default branch",
                RULE_QUEUE,
                lambda: self.mutate('origin "HEAD:refs/heads/$branch"', 'origin "HEAD:refs/heads/main"'),
                lambda c: c.check_revert_lands_in_the_queue(),
            ),
            (
                "a `declared` input that opts a repository in",
                RULE_OPT_IN,
                lambda: self.insert_entries("inputs:", [
                    "declared:",
                    "  description: Whether the repository declared optimistic merge.",
                    "  type: boolean",
                    "  default: false",
                ]),
                lambda c: c.check_opt_in_only(),
            ),
            (
                "a write permission on the default token",
                RULE_GRANT,
                lambda: self.insert_entries("permissions:", ["issues: write"], last=True),
                lambda c: c.check_grant_and_audit(),
            ),
            (
                "a silent audit trail",
                RULE_GRANT,
                lambda: self.mutate("always() &&", "success() &&"),
                lambda c: c.check_grant_and_audit(),
            ),
            (
                "a second queue jump",
                RULE_QUEUE,
                lambda: self.mutate(
                    r"dequeuePullRequest(input:{id:\$id})",
                    r"dequeuePullRequest(input:{id:\$id,jump:true})",
                ),
                lambda c: c.check_revert_lands_in_the_queue(),
            ),
        )

        for name, rule, build, check in mutations:
            with self.subTest(mutation=name):
                mutated = build()
                self.assertNotEqual(mutated, self.text, f"{name}: the mutation changed nothing")
                with self.assertRaises(ContractViolation) as caught:
                    check(RedMainContract(mutated))
                message = str(caught.exception)
                self.assertIn(SOURCE, message, f"{name}: the failure does not name the file")
                self.assertRegex(message, rf"^{re.escape(SOURCE)}:\d+:", f"{name}: no line number")
                self.assertIn(rule, message, f"{name}: the failure does not name the rule it breaks")


class TestsWorkflowTest(unittest.TestCase):
    """The repository's own test workflow, held to what it must be."""

    @classmethod
    def setUpClass(cls) -> None:
        if not TESTS_WORKFLOW.is_file():
            raise unittest.SkipTest(f"{TESTS_SOURCE} is not in this checkout")
        cls.text = TESTS_WORKFLOW.read_text()
        cls.doc = yaml.safe_load(cls.text)

    def test_runs_on_pull_requests_merge_groups_and_main(self) -> None:
        on = self.doc.get("on", self.doc.get(True)) or {}
        self.assertIn("pull_request", on, TESTS_SOURCE)
        self.assertIn("merge_group", on, f"{TESTS_SOURCE}: a required check must run on the merge group")
        self.assertIn("main", (on.get("push") or {}).get("branches") or [], TESTS_SOURCE)

    def test_uses_our_own_runner_and_never_a_hosted_label(self) -> None:
        labels = re.findall(r"^\s*runs-on:\s*(.+?)\s*$", self.text, re.MULTILINE)
        self.assertTrue(labels, TESTS_SOURCE)
        for label in labels:
            self.assertEqual(label, "sylphx-linux-standard", f"{TESTS_SOURCE}: runner {label!r}")
        self.assertNotRegex(self.text, r"(ubuntu|windows|macos)-[a-z0-9.-]*", TESTS_SOURCE)

    def test_runs_the_suites_under_one_aggregate(self) -> None:
        self.assertIn("python -m pytest tests/ -q", self.text, TESTS_SOURCE)
        jobs = self.doc.get("jobs") or {}
        self.assertIn("tests-ok", jobs, f"{TESTS_SOURCE}: the aggregate the queue requires")
        aggregate = jobs["tests-ok"]
        test_jobs = {name for name in jobs if name != "tests-ok"}
        self.assertEqual(
            set(aggregate.get("needs") or []), test_jobs,
            f"{TESTS_SOURCE}: tests-ok must wait for every other job; it waits for "
            f"{aggregate.get('needs')!r} and there are {sorted(test_jobs)}",
        )
        self.assertIn("always()", aggregate.get("if") or "", f"{TESTS_SOURCE}: tests-ok runs always()")


if __name__ == "__main__":
    unittest.main()
