#!/usr/bin/env python3
"""The iOS release workflow: cleanup always runs, secrets are never echoed,
the keychain password is masked, and review submission is structurally absent."""

from __future__ import annotations

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ios-release.yml"

SECRETS = (
    "APP_STORE_CONNECT_API_KEY_ID",
    "APP_STORE_CONNECT_ISSUER_ID",
    "APP_STORE_CONNECT_API_PRIVATE_KEY_BASE64",
    "IOS_DIST_CERTIFICATE_BASE64",
    "IOS_DIST_CERTIFICATE_PASSWORD",
    "APPLE_TEAM_ID",
    "IOS_PROVISIONING_PROFILE_BASE64",
)
# Anything that could submit for review or release a build. TestFlight upload only.
FORBIDDEN = (
    "appstoreversionsubmissions",
    "reviewsubmissions",
    "appstoreversions",
    "appstoreversionphasedreleases",
    "phased",
    "betaappreviewsubmissions",
    "appstoreversionreleaserequests",
    "itmstransporter",
    "pilot distribute",
    "apple-tool:",
    "-t /usr/bin/security",
    "--submit",
    "submit_for_review",
    "submit-for-review",
    "release-to-store",
    "api.appstoreconnect.apple.com",
    "pilot",
    "fastlane",
)


class IosRelease(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text()
        self.doc = yaml.safe_load(self.text)
        self.job = self.doc["jobs"]["release"]
        self.steps = self.job["steps"]
        # PyYAML reads the key `on` as True.
        self.call = (self.doc.get("on") or self.doc[True])["workflow_call"]

    def step(self, prefix: str) -> dict:
        found = [s for s in self.steps if s["name"].startswith(prefix)]
        self.assertEqual(len(found), 1, prefix)
        return found[0]

    def test_cleanup_is_the_last_step_and_always_runs(self) -> None:
        last = self.steps[-1]
        self.assertTrue(last["name"].startswith("Cleanup"))
        self.assertEqual(last["if"], "always()")

    def test_cleanup_removes_keychain_profiles_key_and_temp_files(self) -> None:
        body = self.step("Cleanup")["run"]
        for needle in ("delete-keychain", "list-keychains", "installed.list", "private_keys", 'rm -rf "$dir"'):
            self.assertIn(needle, body)

    def test_every_step_has_a_timeout_and_no_swallowed_failure(self) -> None:
        for s in self.steps:
            self.assertIn("timeout-minutes", s, s["name"])
        self.assertIn("timeout-minutes", self.job)
        self.assertNotIn("|| true", self.text)
        self.assertNotIn("continue-on-error", self.text)

    def test_cleanup_is_per_item_not_aborting(self) -> None:
        body = self.step("Cleanup")["run"]
        self.assertNotIn("set -e", body)
        self.assertGreaterEqual(body.count("|| fail"), 5)
        self.assertIn('exit "$failed"', body)

    def test_no_ref_input_and_environment_is_wired(self) -> None:
        self.assertNotIn("ref", self.call["inputs"])
        self.assertEqual(self.job["environment"], "${{ inputs.environment }}")
        self.assertEqual(self.call["inputs"]["environment"]["default"], "")

    def test_api_key_lives_under_runner_temp_and_after_the_build(self) -> None:
        self.assertNotIn(".appstoreconnect", self.text)
        self.assertNotIn("$HOME/.appstoreconnect", self.text)
        names = [s["name"] for s in self.steps]
        writers = [s["name"] for s in self.steps if "APP_STORE_CONNECT_API_PRIVATE_KEY_BASE64" in str(s.get("env"))]
        self.assertEqual(writers, ["Upload to TestFlight"])
        self.assertGreater(names.index("Upload to TestFlight"), names.index("Export"))

    def test_pre_build_runs_before_signing_and_holds_no_secret(self) -> None:
        names = [s["name"] for s in self.steps]
        pre = names.index(self.step("Pre-build script")["name"])
        self.assertLess(names.index(self.step("Download Xcode")["name"]), pre)
        self.assertLess(pre, names.index(self.step("Signing setup")["name"]))
        step = self.step("Pre-build script")
        self.assertNotIn("secrets.", str(step.get("env")))
        self.assertNotIn("secrets.", str(self.job.get("env")))
        self.assertNotIn("${{", step["run"])
        self.assertIn('bash -- "$IN_PRE_BUILD_SCRIPT"', step["run"])
        self.assertIn("pwd -P", step["run"])
        for s in self.steps[: names.index(step["name"]) + 1]:
            if s["name"] != "Validate inputs":
                self.assertNotIn("secrets.", str(s.get("env")), s["name"])

    def test_path_validation_rejects_dotdot_and_absolute(self) -> None:
        body = self.step("Validate inputs")["run"]
        self.assertIn("/* | *..*) return 1", body)
        for var in ("IN_ARTIFACT_PATH", "IN_PRE_BUILD_SCRIPT", "IN_XCCONFIG"):
            self.assertIn(var, body)
        # Run the real validator on samples.
        import subprocess
        fn = re.search(r"path_ok\(\) \{.*?\n\}", body, re.S).group(0)
        for value, ok in (("a/b.sh", True), (".", True), ("../x", False), ("a/../b", False), ("/etc/passwd", False)):
            r = subprocess.run(["bash", "-c", fn + '\npath_ok "$1"', "x", value])
            self.assertEqual(r.returncode == 0, ok, value)

    def test_prebuilt_mode_skips_the_build_steps_and_validates(self) -> None:
        for prefix in ("Checkout", "Signing setup", "Archive", "Export"):
            self.assertEqual(self.step(prefix)["if"], "inputs.ipa-artifact == ''")
        download = self.step("Download prebuilt")
        self.assertEqual(download["if"], "inputs.ipa-artifact != ''")
        self.assertRegex(download["uses"], r"^actions/download-artifact@[0-9a-f]{40}$")
        validate = self.step("Validate prebuilt")
        self.assertEqual(validate["if"], "inputs.ipa-artifact != ''")
        body = validate["run"]
        for needle in (
            "CFBundleIdentifier", "Authority=Apple Distribution:", "codesign --verify --deep --strict",
            "beta-reports-active", "ProvisionedDevices", "application-identifier", "CFBundleShortVersionString",
            "exactly one .ipa",
        ):
            self.assertIn(needle, body)
        # The shared upload step has no mode condition.
        self.assertNotIn("if", self.step("Upload to TestFlight"))

    def test_signing_secrets_optional_asc_secrets_required(self) -> None:
        secrets = self.call["secrets"]
        for name in ("IOS_DIST_CERTIFICATE_BASE64", "IOS_DIST_CERTIFICATE_PASSWORD", "APPLE_TEAM_ID", "IOS_PROVISIONING_PROFILE_BASE64"):
            self.assertFalse(secrets[name]["required"], name)
        for name in ("APP_STORE_CONNECT_API_KEY_ID", "APP_STORE_CONNECT_ISSUER_ID", "APP_STORE_CONNECT_API_PRIVATE_KEY_BASE64"):
            self.assertTrue(secrets[name]["required"], name)
        body = self.step("Validate inputs")["run"]
        self.assertIn("build mode needs the signing secrets", body)

    def test_keychain_password_is_random_and_masked_before_use(self) -> None:
        body = self.step("Signing setup")["run"]
        self.assertIn("openssl rand", body)
        self.assertLess(body.index("::add-mask::$kc_password"), body.index("create-keychain"))
        self.assertIn("set-keychain-settings -lut 5400", body)
        self.assertIn("set-key-partition-list -S codesign: -s -k", body)
        self.assertIn("security list-keychains -d user -s", body)

    def test_import_grants_only_codesign(self) -> None:
        body = self.step("Signing setup")["run"]
        self.assertEqual(re.findall(r"-T (\S+)", body), ["/usr/bin/codesign"])

    def test_no_step_echoes_a_secret(self) -> None:
        self.assertNotIn("set -x", self.text)
        self.assertNotIn("xtrace", self.text)
        for s in self.steps:
            env = s.get("env") or {}
            names = [k for k, v in env.items() if "secrets." in str(v)]
            for line in s.get("run", "").splitlines():
                if re.match(r"\s*(echo|printf)\b", line) and "|" not in line and ">" not in line:
                    for n in names:
                        self.assertNotIn(f"${n}", line, s["name"])
                        self.assertNotIn(f"${{{n}}}", line, s["name"])
                    if "::add-mask::" not in line:
                        self.assertNotIn("$kc_password", line, s["name"])
        # printf of a secret is only ever piped into base64 --decode.
        for m in re.finditer(r"printf '%s' \"\$(\w+)\"([^\n]*)", self.text):
            self.assertIn("base64 --decode", m.group(2), m.group(1))

    def test_secrets_only_appear_in_step_env_never_in_run_bodies(self) -> None:
        for s in self.steps:
            self.assertNotIn("${{", s.get("run", ""), s["name"])
        for s in self.steps:
            for _k, v in (s.get("env") or {}).items():
                if "secrets." in str(v):
                    self.assertRegex(str(v), r"^\$\{\{ secrets\.[A-Z0-9_]+( != '')? \}\}$")

    def test_interface_requires_what_signing_needs(self) -> None:
        secrets = self.call["secrets"]
        self.assertEqual(sorted(secrets), sorted(SECRETS))
        inputs = self.call["inputs"]
        self.assertEqual(inputs["scheme"]["default"], "")
        self.assertTrue(inputs["bundle-id"]["required"])
        self.assertNotIn("inherit", self.text)

    def test_runs_on_the_internal_macos_class(self) -> None:
        self.assertEqual(self.job["runs-on"], ["self-hosted", "macos", "sylphx", "standard"])

    def test_signing_and_export_settings(self) -> None:
        archive = self.step("Archive")["run"]
        for needle in ("CODE_SIGN_STYLE=Manual", "CODE_SIGN_IDENTITY=Apple Distribution", "PROVISIONING_PROFILE_SPECIFIER"):
            self.assertIn(needle, archive)
        export = self.step("Export")["run"]
        for needle in ("app-store-connect", "signingStyle string manual", "provisioningProfiles", "-exportArchive"):
            self.assertIn(needle, export)

    def test_files_are_owner_only(self) -> None:
        self.assertIn("umask 077", self.step("Signing setup")["run"])
        self.assertIn("chmod 600", self.step("Upload to TestFlight")["run"])

    def test_testflight_upload_only_review_and_release_are_absent(self) -> None:
        low = self.text.lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, low, word)
        self.assertIsNone(re.search(r"\bdeliver\b", low))
        # `altool --upload-app` is the single upload call.
        self.assertEqual(len(re.findall(r"xcrun altool", self.text)), 1)
        self.assertIn("xcrun altool --upload-app", self.text)


if __name__ == "__main__":
    unittest.main()
