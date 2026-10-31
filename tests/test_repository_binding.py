import importlib.util
import json
from pathlib import Path
import textwrap

import pytest

SPEC = importlib.util.spec_from_file_location(
    "repository_binding", Path(__file__).parents[1] / ".github/actions/repository-binding/check.py")
binding = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(binding)
OWNERS = ["Old-Org"]


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def findings(root, declarations=()):
    return binding.check(root, OWNERS, list(declarations))


@pytest.mark.parametrize("line", [
    "repository: Old-Org/game",
    "owner: Old-Org",
    "owner: old-org",
    "url: https://github.com/Old-Org/game",
    "uses: Old-Org/action/.github/workflows/x.yml@main",
    "image: registry.sylphx.com/old-org/app and repository: Old-Org/game",
])
def test_retired_owner_references_are_refused(tmp_path, line):
    write(tmp_path, ".github/workflows/a.yml", line + "\n")
    assert len(findings(tmp_path)) == 1


@pytest.mark.parametrize("line", [
    "image: registry.sylphx.com/old-org/app",
    "image: ghcr.io/old-org/app:latest",
    "repository: New-Org/game",
    "repository: Old-Org-archive/game",
    "repository-id: '179024719'",
])
def test_storage_paths_and_other_owners_pass(tmp_path, line):
    write(tmp_path, ".github/workflows/a.yml", line + "\n")
    assert findings(tmp_path) == []


def test_the_callers_own_input_passes(tmp_path):
    write(tmp_path, ".github/workflows/a.yml", "        with:\n          legacy-owners: Old-Org\n")
    assert findings(tmp_path) == []


def test_submodules_and_composite_actions_are_read(tmp_path):
    write(tmp_path, ".gitmodules", "url = https://github.com/Old-Org/game.git\n")
    write(tmp_path, ".github/actions/x/action.yml", "owner: Old-Org\n")
    assert len(findings(tmp_path)) == 2


def test_relative_submodule_url_passes(tmp_path):
    write(tmp_path, ".gitmodules", "url = ../game.git\n")
    assert findings(tmp_path) == []


def test_other_source_files_are_out_of_scope(tmp_path):
    write(tmp_path, "docs/history.md", "Imported from Old-Org/game\n")
    assert findings(tmp_path) == []


@pytest.mark.parametrize("line", [
    "if: github.repository == 'New-Org/game'",
    "if: github.repository_owner != 'New-Org'",
    "if: 'New-Org/game' == github.repository",
])
def test_name_tests_are_refused(tmp_path, line):
    write(tmp_path, ".github/workflows/a.yml", line + "\n")
    assert len(findings(tmp_path)) == 1


def test_id_test_passes(tmp_path):
    write(tmp_path, ".github/workflows/a.yml", "if: github.repository_id == 7\n")
    assert findings(tmp_path) == []


@pytest.mark.parametrize("entry,refused", [
    ({"name": "a", "repository": "x/y", "workflow": ".github/workflows/a.yml"}, True),
    ({"name": "a", "repository": "x/y", "repository_id": "7", "workflow": "w"}, True),
    ({"name": "a", "repository": "x/y", "repository_id": True, "workflow": "w"}, True),
    ({"name": "a", "repository": "x/y", "repository_id": 7, "workflow": "w"}, False),
    ({"name": "a", "repository": "x/y", "repositoryId": 7, "workflow": "w"}, False),
    ({"name": "a", "repository_id": 7, "workflow": "w"}, False),
    ({"repository": "SylphxAI/.github", "commit": "a"}, False),
    ({"sourceRepository": "x/y", "sourceWorkflow": "w"}, True),
    ({"sourceRepository": "x/y", "sourceRepositoryId": 7}, False),
    ({"sourceRepositoryId": 7}, False),
])
def test_declarations(tmp_path, entry, refused):
    write(tmp_path, "contract.json", json.dumps({"publishers": [entry]}))
    assert bool(findings(tmp_path, ["contract.json"])) is refused


def test_declarations_embedded_in_a_configmap_are_read(tmp_path):
    write(tmp_path, "issuer.yaml", textwrap.dedent("""\
        kind: ConfigMap
        data:
          issuer.py: |
            print("{not json")
          readers.json: |
            {"readers": [{"name": "name-only", "repository": "x/y", "workflow": "w"}]}
          publishers.json: |
            {
              "publishers": [{"name": "bound", "repository_id": 7, "workflow": "w"}]
            }
        ---
        kind: Deployment
        """))
    assert findings(tmp_path, ["issuer.yaml"]) == ["issuer.yaml: name-only binds a repository name without its repository_id"]


def test_a_malformed_declaration_fails_loudly(tmp_path):
    write(tmp_path, "contract.json", "{")
    with pytest.raises(json.JSONDecodeError):
        findings(tmp_path, ["contract.json"])
