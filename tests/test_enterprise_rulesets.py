"""Keep the shared CI scope and publisher binding explicit and minimal."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "policy/enterprise-ci.json").read_text())
PROPERTIES = json.loads((ROOT / "policy/enterprise-properties.json").read_text())


def test_shared_gate_stays_at_enterprise_without_bypasses_or_queue():
    assert POLICY["name"] == "enterprise-required-ci-gate"
    assert POLICY["target"] == "branch"
    assert POLICY["enforcement"] == "active"
    assert POLICY["bypass_actors"] == []
    assert POLICY["rules"] == [{
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": True,
            "required_status_checks": [{"context": "ci-ok", "integration_id": 15368}],
        },
    }]


def test_gate_preserves_org_property_scope_and_excludes_delivered():
    assert POLICY["conditions"] == {
        "organization_name": {"include": ["SylphxAI"], "exclude": []},
        "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
        "repository_property": {
            "include": [{"name": "sylphx_ci_gate", "source": "custom",
                         "property_values": ["required"]}],
            "exclude": [{"name": "sylphx_delivery", "source": "custom",
                         "property_values": ["delivered"]}],
        },
    }


def test_promoted_definitions_preserve_values_and_new_repo_default():
    definitions = {p["property_name"]: p for p in PROPERTIES["properties"]}
    assert set(definitions) == {"sylphx_ci_gate", "sylphx_delivery"}
    ci, delivery = definitions["sylphx_ci_gate"], definitions["sylphx_delivery"]
    assert ci["required"] is True
    assert ci["default_value"] == "required"
    assert ci["allowed_values"] == ["required", "none"]
    assert delivery["required"] is False
    assert "default_value" not in delivery
    assert delivery["allowed_values"] == ["managed", "delivered"]
    for definition in definitions.values():
        assert definition["value_type"] == "single_select"
        assert definition["values_editable_by"] == "org_actors"
        assert definition["require_explicit_values"] is False
