"""Observation must preserve the native result, errno and fatal assertion."""
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/unity-fd-observer"

LIBRARY = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdint.h>
#include <fcntl.h>
#include <unistd.h>
int32_t SystemNative_FcntlSetCloseOnExec(intptr_t fd) {
#ifdef CHECK_ASSERT
    assert(0 <= fd);
    assert(fd < sysconf(_SC_OPEN_MAX));
#endif
    return fcntl((int)fd, F_SETFD, FD_CLOEXEC);
}
'''
DRIVER = r'''
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>
int main(int argc, char **argv) {
    (void)argc;
    void *library = dlopen(argv[1], RTLD_NOW);
    if (!library) return 2;
    int32_t (*call)(intptr_t) = dlsym(library, "SystemNative_FcntlSetCloseOnExec");
    if (!call) return 3;
    if (strcmp(argv[2], "errno") == 0) {
        errno = 0;
        int result = call(-2);
        return result == -1 && errno == EBADF ? 0 : 4;
    }
    struct rlimit original, after;
    if (getrlimit(RLIMIT_NOFILE, &original)) return 5;
    int fd = open("/dev/null", O_RDONLY);
    if (strcmp(argv[2], "assert") == 0) {
        struct rlimit high = {4096, 4096};
        if (setrlimit(RLIMIT_NOFILE, &high)) return 6;
        fd = dup2(fd, 3000);
        high.rlim_cur = 1024;
        if (setrlimit(RLIMIT_NOFILE, &high)) return 7;
    }
    int result = call(fd);
    if (getrlimit(RLIMIT_NOFILE, &after)) return 8;
    if (original.rlim_cur != after.rlim_cur || original.rlim_max != after.rlim_max) return 9;
    printf("fd=%d result=%d\n", fd, result);
    return result;
}
'''


def no_core():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


@unittest.skipUnless(shutil.which("cc") and os.uname().machine == "x86_64", "requires Linux x86_64 C compiler")
class NativeObservation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        cls.audit = cls.directory / "audit.so"
        (cls.directory / "fixture.c").write_text(LIBRARY)
        (cls.directory / "driver.c").write_text(DRIVER)
        subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror", "-O2",
                        str(ACTION / "audit.c"), "-o", str(cls.audit)], check=True)
        for kind in ("assert", "errno"):
            command = ["cc", "-shared", "-fPIC", str(cls.directory / "fixture.c"),
                       "-o", str(cls.directory / (kind + ".so"))]
            if kind == "assert":
                command.append("-DCHECK_ASSERT")
            subprocess.run(command, check=True)
        subprocess.run(["cc", str(cls.directory / "driver.c"), "-ldl", "-o", str(cls.directory / "driver")], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_driver(self, mode, observed):
        env = dict(os.environ)
        env.pop("LD_AUDIT", None)
        if observed:
            env["LD_AUDIT"] = str(self.audit)
        library = "errno" if mode == "errno" else "assert"
        return subprocess.run([str(self.directory / "driver"), str(self.directory / (library + ".so")), mode],
                              env=env, capture_output=True, text=True, preexec_fn=no_core)

    def test_passing_native_result_and_limits_unchanged(self):
        baseline = self.run_driver("pass", False)
        observed = self.run_driver("pass", True)
        self.assertEqual((baseline.returncode, observed.returncode), (0, 0))
        self.assertEqual(baseline.stdout, observed.stdout)
        self.assertIn("event=process-start", observed.stderr)
        self.assertIn("event=named-pipe-close-on-exec", observed.stderr)
        self.assertIn("fd_known=1", observed.stderr)

    def test_native_errno_preserved(self):
        self.assertEqual(self.run_driver("errno", False).returncode, 0)
        self.assertEqual(self.run_driver("errno", True).returncode, 0)

    def test_stranded_descriptor_recorded_without_suppressing_abort(self):
        baseline = self.run_driver("assert", False)
        observed = self.run_driver("assert", True)
        self.assertEqual((baseline.returncode, observed.returncode), (-signal.SIGABRT, -signal.SIGABRT))
        self.assertIn("event=named-pipe-close-on-exec", observed.stderr)
        self.assertIn("fd=3000 fd_known=1 soft=1024 hard=4096 sysconf=1024", observed.stderr)
        self.assertRegex(observed.stderr, r"event=assertion .*fd=3000 fd_known=1 soft=1024 hard=4096 sysconf=1024")


class DockerSelection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.wrapper = self.directory / "docker"
        shutil.copyfile(ACTION / "docker.py", self.wrapper)
        self.fake = self.directory / "real-docker"
        self.fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\nopen(os.environ['ARGUMENTS'], 'w').write(json.dumps(sys.argv[1:]))\nsys.exit(37)\n")
        self.fake.chmod(0o755)
        (self.directory / "config.json").write_text(json.dumps({"docker": str(self.fake), "library": str(self.directory / "audit.so")}))

    def tearDown(self):
        self.temp.cleanup()

    def run_wrapper(self, args, image="example/editor:test"):
        output = self.directory / "args.json"
        env = dict(os.environ, ARGUMENTS=str(output), UNITY_IMAGE=image)
        result = subprocess.run(["python3", str(self.wrapper), *args], env=env, capture_output=True, text=True)
        return result, json.loads(output.read_text()) if output.exists() else None

    def test_matching_image_gets_read_only_observer_and_original_limits(self):
        original = ["run", "--rm", "--ulimit", "nofile=8192:8192", "example/editor:test", "-quit"]
        result, args = self.run_wrapper(original)
        self.assertEqual(result.returncode, 37)
        self.assertEqual(args[0], "run")
        self.assertEqual(args[1:5], ["--volume", str(self.directory / "audit.so") + ":/opt/unity-fd-observer.so:ro",
                                   "--env", "LD_AUDIT=/opt/unity-fd-observer.so"])
        self.assertEqual(args[5:], original[1:])
        self.assertEqual(result.stdout + result.stderr, "")

    def test_other_images_and_non_run_commands_untouched(self):
        for args in (["pull", "example/editor:test"], ["run", "other/editor:test"], ["info"], ["--config", "/tmp", "run", "example/editor:test"]):
            with self.subTest(args=args):
                result, actual = self.run_wrapper(args)
                self.assertEqual((result.returncode, actual), (37, args))

    def test_empty_image_does_not_observe(self):
        args = ["run", "example/editor:test"]
        result, actual = self.run_wrapper(args, image="")
        self.assertEqual((result.returncode, actual), (37, args))

    def test_existing_audit_policy_is_not_replaced(self):
        result, actual = self.run_wrapper(["run", "--env", "LD_AUDIT=/existing.so", "example/editor:test"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(actual)
        self.assertIn("cannot replace", result.stderr)


class Preparation(unittest.TestCase):
    def test_preparation_builds_and_registers_job_local_wrapper(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            fake = directory / "docker"
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o755)
            path_file = directory / "github-path"
            env = dict(os.environ, RUNNER_TEMP=temp, GITHUB_PATH=str(path_file),
                       PATH=temp + os.pathsep + os.environ["PATH"])
            before = resource.getrlimit(resource.RLIMIT_NOFILE)
            result = subprocess.run(["python3", str(ACTION / "prepare.py")], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = Path(path_file.read_text().strip())
            self.assertTrue((installed / "audit.so").is_file())
            self.assertTrue(os.access(installed / "docker", os.X_OK))
            self.assertEqual(json.loads((installed / "config.json").read_text())["docker"], str(fake))
            self.assertEqual(resource.getrlimit(resource.RLIMIT_NOFILE), before)

    def test_action_is_disabled_by_default(self):
        source = (ACTION / "action.yml").read_text()
        self.assertIn("default: 'false'", source)
        self.assertIn("if: inputs.enabled == 'true'", source)


if __name__ == "__main__":
    unittest.main()
