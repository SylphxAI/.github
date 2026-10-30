"""Source identity is required in each Docker stage that invokes Keel pack."""

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pack_source", ROOT / ".github/actions/keel-repin/pack_source.py")
source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source)


class PackSourceTest(unittest.TestCase):
    def missing(self, docker, files=None):
        files = files or {}
        return source.missing_stages(docker, "Dockerfile.web", set(files), files.get)

    def test_missing_global_other_stage_and_late_arguments_fail(self):
        for before in ("", "ARG SYLPHX_GIT_COMMIT_SHA\n", "FROM rust AS other\nARG SYLPHX_GIT_COMMIT_SHA\n"):
            with self.subTest(before=before):
                docker = before + "FROM rust AS pack\nRUN keel pack --profile web\nARG SYLPHX_GIT_COMMIT_SHA\n"
                self.assertEqual(len(self.missing(docker)), 1)

    def test_stage_argument_and_inherited_argument_pass(self):
        self.assertEqual(self.missing("FROM rust AS base\nARG SYLPHX_GIT_COMMIT_SHA\n"
            "RUN keel pack --profile web\nFROM BASE AS pack\nRUN keel pack --profile web\n"
            "FROM nginx\nCOPY --from=pack /out /srv\n"), [])
        self.assertEqual(self.missing("FROM rust\nARG SYLPHX_GIT_COMMIT_SHA\n"
                                     "FROM 0\nRUN keel pack --profile web\n"), [])

    def test_only_packing_stages_are_reported_once_with_actionable_lines(self):
        self.assertEqual(self.missing("FROM rust AS build\nRUN cargo build\n"
            "FROM rust AS pack\nRUN keel pack --profile web\nRUN keel pack --profile web\n"
            "FROM nginx\nCOPY --from=pack /out /srv\n"), [(3, 4, "pack")])

    def test_wrappers_continuations_mounts_and_cycles_are_followed_without_execution(self):
        files = {"scripts/web.sh": 'bash scripts/pack.sh "$@"\n',
                 "scripts/pack.sh": 'bash scripts/web.sh\n"${KEEL_CLI:-keel}" pack --profile web\n'}
        self.assertEqual(self.missing("FROM rust AS pack\nRUN --mount=type=cache,target=/cache \\\n    bash scripts/web.sh /out\n", files), [(1, 2, "pack")])
        self.assertEqual(self.missing("FROM rust AS pack\nRUN --mount=type=cache,target=/cache keel pack\n"),
                         [(1, 2, "pack")])

    def test_exec_form_and_shell_command_form_are_checked(self):
        for run in ('["keel", "pack", "--profile", "web"]',
                    '["sh", "-c", "keel pack --profile web"]',
                    'sh -c "keel pack --profile web"', '/usr/local/bin/keel pack --profile web'):
            with self.subTest(run=run):
                self.assertEqual(self.missing("FROM rust AS pack\nRUN " + run + "\n"), [(1, 2, "pack")])

    def test_wrapper_can_keep_pack_arguments_in_a_literal_array(self):
        files = {"scripts/web.sh": 'ARGS=(pack --profile web)\n"$KEEL" "${ARGS[@]}"\n'}
        self.assertEqual(self.missing("FROM rust AS web\nRUN bash scripts/web.sh\n", files), [(1, 2, "web")])

    def test_shell_parameter_expansion_is_not_a_comment(self):
        docker = 'FROM rust AS pack\nRUN pair=x; org=${pair#* }; keel pack --profile web\n'
        self.assertEqual(self.missing(docker), [(1, 2, "pack")])
        self.assertEqual(self.missing('FROM rust\nRUN true # ; keel pack\n'), [])

    def test_native_and_inspection_commands_need_no_web_identity(self):
        for arguments in ("--profile android --release", "--profile=ios --release", "-p extension",
                          "--verify /out", "--verify=/out", "--list-profiles", "--update-keygen"):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.missing("FROM rust\nRUN keel pack " + arguments + "\n"), [])

    def test_comments_documentation_and_unexecuted_script_names_pass(self):
        files = {"scripts/web.sh": "keel pack --profile web\n"}
        docker = ("FROM rust\n# RUN keel pack\nRUN echo 'keel pack'\n"
                  "RUN printf '%s' scripts/web.sh\nRUN cat scripts/web.sh\n"
                  "COPY scripts/web.sh /tmp/web.sh\nRUN cargo build\n")
        self.assertEqual(self.missing(docker, files), [])

    def test_comment_in_wrapper_is_not_a_pack_and_relative_wrapper_is_followed(self):
        files = {"scripts/web.sh": "# keel pack\necho 'keel pack'\n"}
        docker = "FROM rust\nRUN bash scripts/web.sh\n"
        self.assertEqual(self.missing(docker, files), [])
        files.update({"scripts/web.sh": "bash ./pack.sh\n", "scripts/pack.sh": "keel pack\n"})
        self.assertEqual(len(self.missing(docker, files)), 1)

    def test_supported_names_and_ignored_dependency_outputs(self):
        for path in ("Dockerfile", "Dockerfile.web", "web.Dockerfile", "crates/api/Dockerfile.static"):
            self.assertTrue(source.dockerfile(path), path)
        for path in ("Dockerfile.dockerignore", "vendor/example/Dockerfile", "target/Dockerfile"):
            self.assertFalse(source.dockerfile(path), path)


if __name__ == "__main__":
    unittest.main()
