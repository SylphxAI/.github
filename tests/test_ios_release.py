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
        self.job = self.doc["jobs"]["sign"]
        self.upload = self.doc["jobs"]["upload"]
        self.prepare = self.doc["jobs"]["prepare"]
        self.steps = self.job["steps"]
        # PyYAML reads the key `on` as True.
        self.call = (self.doc.get("on") or self.doc[True])["workflow_call"]

    def step(self, prefix: str, job: dict | None = None) -> dict:
        found = [s for s in (job or self.job)["steps"] if s["name"].startswith(prefix)]
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
        for s in self.prepare["steps"]:
            self.assertIn("timeout-minutes", s, s["name"])
        self.assertIn("timeout-minutes", self.prepare)
        self.assertNotIn("|| true", self.text)
        # The one allowed form: an upload may not fail a merge_group run (org
        # artifact quota); outside merge_group it stays loud.
        allowed = "continue-on-error: ${{ github.event_name == 'merge_group' }}"
        for line in self.text.splitlines():
            if "continue-on-error" in line and not line.lstrip().startswith("#"):
                self.assertEqual(line.strip(), allowed)

    def test_cleanup_is_per_item_not_aborting(self) -> None:
        body = self.step("Cleanup")["run"]
        self.assertNotIn("set -e", body)
        self.assertGreaterEqual(body.count("|| fail"), 5)
        self.assertIn('exit "$failed"', body)

    def test_no_ref_input_and_only_the_signing_job_has_the_environment(self) -> None:
        self.assertNotIn("ref", self.call["inputs"])
        self.assertEqual(self.job["environment"], "${{ inputs.environment }}")
        self.assertNotIn("environment", self.prepare)
        self.assertEqual(self.call["inputs"]["environment"]["default"], "ios-release")
        self.assertEqual(self.job["needs"], "prepare")
        for j in (self.job, self.prepare):
            body = self.step("Validate inputs", j)["run"]
            self.assertIn("A-Za-z0-9._-]{1,64}", body)
            self.assertIn("environment must be set", body)

    def test_no_secrets_in_the_interface_and_none_in_prepare(self) -> None:
        self.assertNotIn("secrets", self.call)
        self.assertNotIn("secrets.", yaml.safe_dump(self.prepare))
        self.assertNotIn("secrets.", yaml.safe_dump(self.doc["jobs"]["prepare"].get("env", {})))
        self.assertNotIn("secrets.", str(self.job.get("env")))

    def test_required_secrets_steps_name_the_secret_and_the_environment(self) -> None:
        sign = self.step("Require secrets")
        for name in SECRETS[3:]:
            self.assertIn(f"HAVE_{name}", sign["env"])
        up = self.step("Require secrets", self.upload)
        for name in SECRETS[:3]:
            self.assertIn(f"HAVE_{name}", up["env"])
        for s in (sign, up):
            self.assertIn("in the '$IN_ENVIRONMENT' environment", s["run"])

    def test_process_guard_after_each_script_and_before_the_key(self) -> None:
        names = [s["name"] for s in self.steps]
        idx = lambda p: names.index(self.step(p)["name"])  # noqa: E731
        self.assertLess(idx("Archive"), idx("Process guard after archive"))
        self.assertLess(idx("Process guard after archive"), idx("Post-archive script"))
        self.assertLess(idx("Post-archive script"), idx("Process guard after post-archive"))
        self.assertLess(idx("Process guard after post-archive"), idx("Export"))
        self.assertLess(idx("Export"), idx("Process guard after export"))
        self.assertLess(idx("Process guard after export"), idx("Upload exported .ipa"))
        for p in ("Process guard after archive", "Process guard after post-archive", "Process guard after export"):
            self.assertIn("guard.sh\" kill", self.step(p)["run"])
            self.assertIn("shasum -a 256 -c", self.step(p)["run"])
        guard = self.step("Validate inputs")["run"]
        self.assertIn("baseline", guard)
        self.assertIn("kill -TERM", guard)
        self.assertIn("kill -KILL", guard)
        self.assertIn("sleep 5", guard)

    def guard_text(self) -> str:
        return re.search(r"<<'GUARD'\n(.*?)\nGUARD", self.step("Validate inputs")["run"], re.S).group(1) + "\n"

    def test_guard_preflight_runs_first_and_refuses_off_a_ci_runner(self) -> None:
        import os
        import subprocess
        import tempfile

        text = self.guard_text()
        # Static order: the entry point runs preflight before main, and the
        # preflight names no ps, kill or process listing.
        self.assertLess(text.index("preflight || exit 1"), text.index('main "$1"'))
        pre = re.search(r"preflight\(\) \{.*?\n\}", text, re.S).group(0)
        for word in ("ps ", "kill", "pgrep", "snap"):
            self.assertNotIn(word, pre)
        for needle in ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT", "runner-[0-9a-f]{12}", "desk-0|kyle-desk", "/home/kyle", "USER"):
            self.assertIn(needle, pre)
        # Source only the preflight, with ps and kill stubbed to a marker.
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "pre.sh")
            with open(script, "w") as f:
                f.write("ps() { echo CALLED_PS; return 1; }\nkill() { echo CALLED_KILL; return 1; }\n" + pre + "\n")
            def run(env: dict[str, str]) -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["bash", "-c", f"source {script}; preflight"],
                    env={"PATH": "/usr/bin:/bin", **env}, capture_output=True, text=True,
                )
            good = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted",
                    "RUNNER_NAME": "runner-0123456789ab", "HOME": "/Users/runner", "USER": "runner"}
            for bad in (
                {}, {**good, "GITHUB_ACTIONS": "false"}, {**good, "RUNNER_ENVIRONMENT": ""},
                {**good, "RUNNER_NAME": "kyle-desk"}, {**good, "RUNNER_NAME": "runner-XYZ"},
                {**good, "HOME": "/home/kyle"}, {**good, "USER": "kyle"},
            ):
                r = run(bad)
                self.assertNotEqual(r.returncode, 0, bad)
                self.assertIn("refusing: not a single-use CI runner", r.stdout)
                self.assertNotIn("CALLED", r.stdout + r.stderr)
            # Host check: a desk hostname refuses even with a perfect environment.
            r = subprocess.run(
                ["bash", "-c", f"hostname() {{ echo desk-0; }}; source {script}; preflight"],
                env={"PATH": "/usr/bin:/bin", **good}, capture_output=True, text=True,
            )
            self.assertNotEqual(r.returncode, 0)
            r = subprocess.run(
                ["bash", "-c", f"hostname() {{ echo mac-abc; }}; source {script}; preflight"],
                env={"PATH": "/usr/bin:/bin", **good}, capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_post_archive_script_between_archive_and_export_without_secrets(self) -> None:
        step = self.step("Post-archive script")
        self.assertNotIn("secrets.", str(step.get("env")))
        self.assertNotIn("${{", step["run"])
        self.assertIn('bash -- "$IN_POST_ARCHIVE_SCRIPT"', step["run"])
        self.assertIn("-u GITHUB_ENV", step["run"])
        self.assertIn("path_ok", self.step("Validate inputs")["run"])
        self.assertIn("IN_POST_ARCHIVE_SCRIPT", self.step("Validate inputs")["run"])

    def test_archive_has_no_global_signing_overrides(self) -> None:
        for s in self.steps:
            if s["name"] == "Archive":
                body = s["run"]
                self.assertNotIn("CODE_SIGN_IDENTITY=", body)
                self.assertNotIn("PROVISIONING_PROFILE_SPECIFIER=", body)
                self.assertNotIn("CODE_SIGN_STYLE=", body)
                self.assertIn('[ "$IN_OVERRIDE_TEAM" != true ] ||', body)

    def test_xcode_version_is_validated_and_selected(self) -> None:
        body = self.step("Select Xcode version")["run"]
        self.assertIn("DEVELOPER_DIR=", body)
        self.assertIn("installed:", body)
        self.assertIn("xcode-version", self.step("Validate inputs")["run"])
        import subprocess
        m = re.search(r'\[\[ "\$IN_XCODE_VERSION" =~ (\S+) \]\]', self.step("Validate inputs")["run"])
        for value, ok in (("26.3", True), ("", True), ("26.3.1", True), ("26;rm", False), ("x", False), ("1.2.3.4", False)):
            r = subprocess.run(["bash", "-c", f'[[ "$1" =~ {m.group(1)} ]]', "x", value])
            self.assertEqual(r.returncode == 0, ok, value)

    def test_asc_secrets_and_the_key_live_only_in_the_upload_job(self) -> None:
        self.assertNotIn(".appstoreconnect", self.text)
        for j in (self.prepare, self.job):
            dump = yaml.safe_dump(j)
            self.assertNotIn("APP_STORE_CONNECT", dump)
            self.assertNotIn("API_PRIVATE_KEYS_DIR", dump)
            self.assertNotIn("altool", dump)
        dump = yaml.safe_dump(self.upload)
        self.assertIn("APP_STORE_CONNECT_API_PRIVATE_KEY_BASE64", dump)
        writers = [s["name"] for s in self.upload["steps"] if "APP_STORE_CONNECT_API_PRIVATE_KEY_BASE64" in str(s.get("env")) and s["name"] != "Require secrets"]
        self.assertEqual(writers, ["Upload to TestFlight"])
        names = [s["name"] for s in self.upload["steps"]]
        key = names.index("Upload to TestFlight")
        self.assertLess(names.index("Validate .ipa before upload"), key)
        self.assertLess(names.index("Process check before upload"), key)
        self.assertLess(names.index("Download .ipa"), names.index("Validate .ipa before upload"))
        body = self.step("Upload to TestFlight", self.upload)["run"]
        self.assertLess(body.index('guard.sh" check'), body.index("base64 --decode"))

    def test_upload_job_has_the_environment_and_follows_sign(self) -> None:
        self.assertEqual(self.upload["environment"], self.job["environment"])
        self.assertEqual(self.upload["environment"], "${{ inputs.environment }}")
        self.assertEqual(self.upload["needs"], ["prepare", "sign"])
        self.assertEqual(self.job["if"], "inputs.ipa-artifact == ''")
        self.assertIn("needs.sign.result == 'skipped'", self.upload["if"])
        art = self.step("Upload exported .ipa")
        self.assertRegex(art["uses"], r"^actions/upload-artifact@[0-9a-f]{40}$")
        self.assertEqual(art["with"]["retention-days"], 1)
        self.assertRegex(self.step("Download .ipa", self.upload)["uses"], r"^actions/download-artifact@[0-9a-f]{40}$")

    def test_sign_job_holds_only_the_signing_secrets(self) -> None:
        dump = yaml.safe_dump(self.job)
        for name in ("IOS_DIST_CERTIFICATE_BASE64", "IOS_DIST_CERTIFICATE_PASSWORD", "APPLE_TEAM_ID", "IOS_PROVISIONING_PROFILE_BASE64"):
            self.assertIn(f"secrets.{name}", dump)
        self.assertNotIn("secrets.APP_STORE", dump)
        self.assertNotIn("secrets.IOS_", yaml.safe_dump(self.upload))
        self.assertNotIn("secrets.APPLE_TEAM_ID", yaml.safe_dump(self.upload))

    def test_pre_build_runs_in_prepare_before_any_secret(self) -> None:
        names = [s["name"] for s in self.prepare["steps"]]
        pre = self.step("Pre-build script", self.prepare)
        self.assertLess(names.index(self.step("Download Xcode", self.prepare)["name"]), names.index(pre["name"]))
        self.assertLess(names.index(pre["name"]), names.index(self.step("Pack", self.prepare)["name"]))
        self.assertNotIn("${{", pre["run"])
        self.assertIn('bash -- "$IN_PRE_BUILD_SCRIPT"', pre["run"])
        self.assertIn("-u GITHUB_ENV", pre["run"])
        self.assertIn("pwd -P", pre["run"])
        self.assertNotIn("Pre-build", " ".join(s["name"] for s in self.steps))

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

    def test_prebuilt_mode_skips_sign_and_validates_before_upload(self) -> None:
        self.assertEqual(self.step("Checkout", self.prepare)["if"], "inputs.ipa-artifact == ''")
        self.assertEqual(self.job["if"], "inputs.ipa-artifact == ''")
        body = self.step("Validate .ipa before upload", self.upload)["run"]
        for needle in (
            "CFBundleIdentifier", "Authority=Apple Distribution:", "codesign --verify --deep --strict",
            "beta-reports-active", "ProvisionedDevices", "application-identifier", "CFBundleShortVersionString",
            "exactly one .ipa",
        ):
            self.assertIn(needle, body)
        self.assertNotIn("if", self.step("Upload to TestFlight", self.upload))
        self.assertIn("inputs.ipa-artifact", self.step("Download .ipa", self.upload)["with"]["name"])

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

    def test_interface(self) -> None:
        inputs = self.call["inputs"]
        self.assertTrue(inputs["bundle-id"]["required"])
        for name in ("post-archive-script", "xcode-version", "override-team", "pre-build-script", "xcode-artifact", "ipa-artifact"):
            self.assertIn(name, inputs)
        self.assertFalse(inputs["override-team"]["default"])
        self.assertNotIn("inherit", self.text)

    def test_runs_on_the_internal_macos_class(self) -> None:
        for j in (self.job, self.prepare):
            self.assertEqual(j["runs-on"], ["self-hosted", "macos", "sylphx", "standard"])

    def test_export_options_are_manual_with_profile_map(self) -> None:
        export = self.step("Export")["run"]
        for needle in ("app-store-connect", "signingStyle string manual", "signingCertificate string Apple Distribution", "provisioningProfiles", "-exportArchive"):
            self.assertIn(needle, export)

    def test_files_are_owner_only(self) -> None:
        self.assertIn("umask 077", self.step("Signing setup")["run"])
        self.assertIn("chmod 600", self.step("Upload to TestFlight", self.upload)["run"])

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
