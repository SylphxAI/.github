import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_mechanism_boundary.py"


class MechanismBoundaryTest(unittest.TestCase):
    def test_real_command_refuses_each_literal_but_allows_tenant_declarations(self):
        for literal in ["主腦", "樞紐", "執行器", "domain-facts"]:
            with self.subTest(literal=literal), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                subprocess.run(["git", "init", "-q", tmp], check=True)
                (root / "scripts").mkdir()
                source = root / "scripts/runtime.py"
                source.write_text("# neutral mechanism\n", encoding="utf-8")
                policy = root / "policy.json"
                policy.write_text(json.dumps({"mechanism_roots": ["scripts"], "forbidden_literals": [literal]}), encoding="utf-8")
                (root / "tenant.yaml").write_text(literal, encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=tmp, check=True)
                command = ["python3", str(SCRIPT), "--root", tmp, "--policy", str(policy)]
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                source.write_text(f"# neutral mechanism\n# {literal}\n", encoding="utf-8")
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn("scripts/runtime.py:2:", result.stdout)
                self.assertNotIn(literal, result.stdout)
