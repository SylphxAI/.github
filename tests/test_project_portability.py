"""Literal destinations, executable calls and deployment boundaries."""
import unittest

from test_stack_conformance import sc


def scan(path, text):
    return [row for row in sc.architecture.scan_file(path, text, 'example/app', set(), [])
            if row['rule'] == 'project-portability']


class ProjectPortabilityTests(unittest.TestCase):
    def test_private_destinations_and_templates(self):
        sources = [
            'const origin = "https://api.tenant.svc.cluster.local";',
            'const origin = "http://api.tenant.svc";',
            'const origin = "http://api.cluster.local";',
            'const origin = `https://${service}.env-${environment}.svc.cluster.local`;',
            'const origin = "https://" + service + ".svc.cluster.local";',
            'const origin = "http://api.tenant.sylphx.internal";',
            'client.post("http://gate:24001/touch");',
            'client.post("http://user:placeholder@gate:24001/touch");',
            'client.get("https://user:placeholder@api.tenant.svc.cluster.local/v1");',
            'const database = "postgresql://user:placeholder@db.tenant.svc.cluster.local/app";',
            'client.get("https://kubernetes.default/api");',
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(len(scan('src/client.ts', source)), 1)

    def test_explicit_in_cluster_calls_and_mounts(self):
        cases = [
            ('src/client.py', 'config.load_incluster_config()'),
            ('src/client.go', 'cfg, err := rest.InClusterConfig()'),
            ('src/client.rs', 'let config = kube::Config::incluster_env()?;'),
            ('src/client.ts', 'kc.loadFromCluster();'),
            ('src/client.cs', 'var config = KubernetesClientConfiguration.InClusterConfig();'),
            ('src/client.rs', 'let token = read("/var/run/secrets/kubernetes.io/serviceaccount/token")?;'),
            ('src/client.py', 'host = os.getenv("KUBERNETES_SERVICE_HOST")'),
            ('src/client.go', 'host := os.Getenv("KUBERNETES_SERVICE_HOST")'),
            ('src/client.ts', 'const host = process.env.KUBERNETES_SERVICE_HOST;'),
        ]
        for path, source in cases:
            with self.subTest(path=path):
                self.assertEqual(len(scan(path, source)), 1)

    def test_public_configurable_clients_and_refusal_suffixes_pass(self):
        cases = [
            ('src/client.ts', 'const origin = config.origin || "https://api.example.invalid";'),
            ('src/client.rs', 'let client = kube::Client::try_default().await?;'),
            ('src/client.ts', 'if (host.endsWith(".svc.cluster.local")) throw Error("private");'),
            ('src/client.rs', 'let private = host.ends_with(".svc") || host.ends_with(".sylphx.internal");'),
            ('src/client.ts', 'const privateSuffixes = [".svc", ".cluster.local", ".sylphx.internal"];'),
            ('src/client.ts', 'throw Error("configure an origin such as http://api.tenant.svc.cluster.local");'),
            ('src/client.rs', 'let value = input.ok_or("example: http://api.tenant.svc.cluster.local")?;'),
            ('src/client.ts', 'client.get("http://localhost:8080/health");'),
            ('src/client.ts', 'client.get("https://api.svc.example.invalid");'),
            ('src/client.py', 'url = "https://x-access-token:%s@github.com/example/repo"'),
            ('src/client.ts', 'client.get("https://user:placeholder@api.example.invalid");'),
            ('src/client.cs', 'var endpoint = "https://api.example.invalid/Billing.svc";'),
            ('src/client.ts', 'client.get("https://bot.svc@api.example.invalid/v1");'),
            ('src/client.ts', 'client.get("https://api.example.invalid/?next=api.tenant.svc.cluster.local");'),
            ('src/client.ts', 'client.get("https://api.example.invalid/kubernetes.default");'),
            ('src/client.sh', 'curl https://api.example.invalid/Billing.svc\n'),
        ]
        for path, source in cases:
            with self.subTest(source=source):
                self.assertEqual(scan(path, source), [])

    def test_comments_and_inline_rust_tests_do_not_hide_adjacent_runtime(self):
        cases = [
            ('src/client.ts', '// "https://api.tenant.svc.cluster.local"\n'),
            ('src/client.py', '"""Example: https://api.tenant.svc.cluster.local"""\n'),
            ('src/client.sh', '# curl http://gate:24001/touch\n'),
            ('src/client.lua', '-- "https://api.tenant.svc.cluster.local"\n'),
            ('src/client.php', '<?php\n# "https://api.tenant.svc.cluster.local"\n'),
            ('apps/api/config.yaml', '# origin: "https://api.tenant.svc.cluster.local"\n'),
        ]
        for path, source in cases:
            with self.subTest(path=path):
                self.assertEqual(scan(path, source), [])
        fixture = '#[cfg(test)] mod tests { const URL: &str = "https://api.tenant.svc.cluster.local"; }\n'
        self.assertEqual(scan('src/client.rs', fixture), [])
        self.assertEqual(len(scan('src/client.rs', fixture + 'const URL: &str = "http://gate:24001/touch";')), 1)

    def test_native_languages_and_unquoted_runtime_shell(self):
        for suffix in ['.jsx', '.kt', '.swift', '.dart', '.cpp', '.h', '.m', '.php', '.rb', '.lua']:
            with self.subTest(suffix=suffix):
                self.assertEqual(len(scan('src/client' + suffix, 'client("http://gate:24001/touch");')), 1)
        self.assertEqual(len(scan('nginx/docker-entrypoint.sh', 'curl http://api.tenant.svc.cluster.local/health\n')), 1)
        self.assertEqual(len(scan('nginx/docker-entrypoint.sh', 'curl "http://api.tenant.svc.cluster.local/health"\n')), 1)

    def test_rust_lifetimes_sql_and_chars_do_not_change_literal_boundaries(self):
        import hashlib
        destination = '"https://api.tenant.svc.cluster.local"'
        source = ("async fn write<'a>(tx: &mut Transaction<'_, Postgres>) {\n"
                  " let sql = r##\"SELECT 'canonical', 'pending', \"quoted\" /* SQL */\"##;\n"
                  " let quote = '\\\''; let unicode = '\\u{1F980}';\n"
                  " let endpoint = " + destination + ";\n}\nfn next<'b>() {}\n")
        row, = scan('src/client.rs', source)
        self.assertEqual(row['line'], 4)
        self.assertEqual(row['end_line'], 4)
        self.assertEqual(row['span_sha256'], hashlib.sha256(destination.encode()).hexdigest())
        # The body may change, but the actual destination literal identity does not.
        edited = source.replace("'canonical'", "'stopped'").replace("Transaction<'_,", "Transaction<'a,")
        self.assertEqual(scan('src/client.rs', edited)[0]['span_sha256'], row['span_sha256'])

    def test_rust_raw_byte_c_strings_and_nested_comments(self):
        for prefix in ['r', 'br', 'cr']:
            with self.subTest(prefix=prefix):
                source = 'let sql = ' + prefix + "##\"SELECT \"literal\", 'pending'; // /* SQL */\nhttp://gate:24001\"##;"
                row, = scan('src/client.rs', source)
                self.assertEqual(row['line'], 1)
                self.assertEqual(row['end_line'], 2)
        for literal in ['"http://gate:24001"', 'b"http://gate:24001"', 'c"http://gate:24001"']:
            self.assertEqual(len(scan('src/client.rs', 'let endpoint = ' + literal + ';')), 1)
        comment = '/* outer /* nested */ "http://hidden:24001" */\n'
        self.assertEqual(scan('src/client.rs', comment), [])
        source = comment + "'retry: loop { let c = '\\x27'; break 'retry; }\nlet endpoint = \"http://gate:24001\";"
        self.assertEqual(len(scan('src/client.rs', source)), 1)
        # Literal contents cannot create executable in-cluster calls.
        self.assertEqual(scan('src/client.rs', 'let sql = r#"Config::incluster_env() /* not a comment */"#;'), [])
        self.assertEqual(len(scan('src/client.rs', "let sql = r#\"SELECT 'pending'\"#; Config::incluster_env();")), 1)

    def test_rust_unterminated_full_source_fails_closed(self):
        for source in ['let x = r##"http://gate"#;', 'let x = "http://gate', '/* http://gate']:
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, 'unterminated Rust'):
                    scan('src/client.rs', source)

    def test_multiline_rust_cache_argument_uses_full_file_comment_spans(self):
        import datetime
        source = ('pub async fn apply_purchase() {\n'
                  ' let offer = state.offer_cache.get(r#"first\nsecond"#).await;\n'
                  ' grant(offer);\n}\n')
        report = sc.architecture.evaluate(sc.DictTree({'src/shop.rs': source}), 'example/app', None,
                                          datetime.date(2026, 11, 3))
        self.assertEqual(report['unreadable'], [])
        self.assertTrue(any(row['rule'] == 'cache-economy' for row in report['findings']))

    def test_rust_balanced_diagnostic_format_arguments_only(self):
        message = '"configure an origin such as https://host:7233"'
        cases = [
            'anyhow::ensure!(url.host_str().is_some_and(|host| !host.is_empty()) && url.port().is_some(), ' + message + ');',
            'anyhow::ensure!(' + 'nested(' * 40 + 'ready' + ')' * 40 + ', ' + message + ');',
            'assert!(ready(), ' + message + ');',
            'assert_eq!(left(), right(), ' + message + ');',
            'tracing::warn!(' + message + ', display(value));',
            'input.ok_or(' + message + ')?;',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(scan('src/client.rs', source), [])
        destination = '"https://api.tenant.svc.cluster.local"'
        sources = [
            'anyhow::ensure!(connect(' + destination + '), ' + message + ');',
            'anyhow::ensure!(ready(), ' + message + ', connect(' + destination + '));',
            'tracing::warn!("{}", connect(' + destination + '));',
            'anyhow::ensure!(ready(), build(' + destination + '));',
            'unknown!(' + destination + ');',
            'ensure(true, ' + destination + ');',
            'assert(true, ' + destination + ');',
            'panic(' + destination + ');',
            'warn(' + destination + ');',
            'assert_eq!(' + destination + ', actual, "unexpected");',
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(len(scan('src/client.rs', source)), 1)
        self.assertEqual(len(scan('src/client.rs', cases[0] + ' connect(' + destination + ');')), 1)

    def test_rust_comparison_vocabulary_is_not_a_destination(self):
        literal = '"/var/run/secrets/kubernetes.io/serviceaccount"'
        cases = ['path == ' + literal, literal + ' != path',
                 'path.starts_with(' + literal + ')', 'path.ends_with(' + literal + ')',
                 'host == "api.tenant.svc.cluster.local"',
                 'host != "http://gate:24001"']
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(scan('src/client.rs', 'fn classify() { let rejected = ' + source + '; }'), [])
        sources = ['read(' + literal + ')', 'path == ' + literal + '.to_owned()',
                   'path.starts_with(read(' + literal + '))',
                   'prefix + ' + literal + ' == path',
                   'format!("{}", ' + literal + ') == path',
                   'path.starts_with(' + literal + ', other)',
                   'path == build(' + literal + ')',
                   'unknown! { path == ' + literal + ' }',
                   'macro_rules! build { () => { path == ' + literal + ' } }',
                   'macro_rules! r#build { () => { path == ' + literal + ' } }',
                   '工具!(path == ' + literal + ')',
                   'bu\u0301ild!(path == ' + literal + ')',
                   '#[build] fn classify() { path == ' + literal + ' }',
                   '#[inline::connect(path == ' + literal + ')] fn classify() {}']
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(len(scan('src/client.rs', source)), 1)

    def test_rust_private_constants_require_every_use_to_be_comparison_only(self):
        declaration = 'const DENIED_PATH: &str = "/var/run/secrets/kubernetes.io/serviceaccount";\n'
        classification = ('fn strip() { mounts.retain(|mount| { let path = mount.path(); '
                          '!(path == DENIED_PATH || path.starts_with(DENIED_PATH)) }); }')
        self.assertEqual(scan('src/client.rs', declaration + classification), [])
        for use in ['read(DENIED_PATH);', 'let alias = DENIED_PATH;',
                    'fn value() -> &str { DENIED_PATH }', 'unknown!(DENIED_PATH);',
                    'host == DENIED_PATH.to_owned();', 'use self::DENIED_PATH;',
                    'let DENIED_PATH = other; path == DENIED_PATH;',
                    'path == module::DENIED_PATH;',
                    'mod external_child;', 'mod r#external_child;', 'mod 工具;', 'build!();',
                    'format!("{DENIED_PATH}");',
                    r'format!("{DENIED_\x50ATH}");', r'format!("{DENIED_\u{50}ATH}");', '#[build] fn emitted() {}', 'include!("more.rs");',
                    'unknown! { path == DENIED_PATH }']:
            with self.subTest(use=use):
                self.assertEqual(len(scan('src/client.rs', declaration + classification + use)), 1)
        for prefix in ['pub ', 'pub(crate) ', 'pub(super) ', '#[build] ', '#[derive(Custom)] ', '#[inline::connect] ']:
            self.assertEqual(len(scan('src/client.rs', prefix + declaration + classification)), 1)
        self.assertEqual(len(scan('src/client.rs', declaration)), 1)
        unicode_source = declaration.replace('DENIED_PATH', 'CAFÉ') + classification.replace('DENIED_PATH', 'CAFÉ')
        self.assertEqual(len(scan('src/client.rs', unicode_source + 'read(CAFE\u0301);')), 1)
        self.assertEqual(len(scan('src/client.rs', 'pub trait Policy { ' + declaration + ' }' + classification)), 1)
        self.assertEqual(len(scan('src/client.rs', 'impl Policy { ' + declaration + ' }' + classification)), 1)
        # Other occurrences and explicit runtime constructors stay independent.
        self.assertEqual(len(scan('src/client.rs', declaration + classification +
                                  ' read("/var/run/secrets/kubernetes.io/serviceaccount"); Config::incluster_env();')), 2)

    def test_rust_environment_presence_does_not_extract_the_endpoint(self):
        for function, methods in [('var_os', ['is_some', 'is_none']), ('var', ['is_ok', 'is_err'])]:
            for method in methods:
                source = 'std::env::' + function + '("KUBERNETES_SERVICE_HOST").' + method + '()'
                with self.subTest(source=source):
                    self.assertEqual(scan('src/status.rs', source), [])
        sources = [
            'std::env::var_os("KUBERNETES_SERVICE_HOST")',
            'var_os("KUBERNETES_SERVICE_HOST").unwrap()',
            'std::env::var_os(' + ' ' * 200 + '"KUBERNETES_SERVICE_HOST").unwrap()',
            'std::env::var("KUBERNETES_SERVICE_PORT").unwrap_or_default()',
            'std::env::var_os("KUBERNETES_SERVICE_HOST").map(connect).is_some()',
            'let value = std::env::var_os("KUBERNETES_SERVICE_HOST"); value.is_some()',
            'std::env::var_os("KUBERNETES_SERVICE_HOST").is_some(value)',
            'other::std::env::var_os("KUBERNETES_SERVICE_HOST").is_some()',
            'std::env::var_os["KUBERNETES_SERVICE_HOST"].is_some()',
            'std::env::var_os{"KUBERNETES_SERVICE_HOST"}.is_some()',
            'unknown!(std::env::var_os("KUBERNETES_SERVICE_HOST").is_some())',
            '工具!(std::env::var_os("KUBERNETES_SERVICE_HOST").is_some())',
            'unknown("KUBERNETES_SERVICE_HOST")',
        ]
        for source in sources[:-1]:
            with self.subTest(source=source):
                self.assertEqual(len(scan('src/client.rs', source)), 1)
        # Unknown context never gains a new exclusion for destinations.
        self.assertEqual(len(scan('src/client.rs', 'unknown("http://gate:24001")')), 1)
        self.assertEqual(len(scan('src/client.rs', 'std::env::var_os("KUBERNETES_SERVICE_HOST").is_some();'
                                  ' connect("http://gate:24001");')), 1)

    def test_rust_unbalanced_context_does_not_suppress_destinations(self):
        for source in ['ensure!(ready(), "http://gate:24001";',
                       'fn f() { host == "http://gate:24001"; ]',
                       'std::env::var_os("KUBERNETES_SERVICE_HOST").is_some(']:
            with self.subTest(source=source):
                self.assertEqual(len(scan('src/client.rs', source)), 1)

    def test_allowlist_is_limited_to_manifests(self):
        manifest = 'origin: "http://api.tenant.svc.cluster.local"\n'
        for path in ['deploy/service.yaml', 'infra/service.yaml', 'charts/service/templates/deployment.yaml', '.github/workflows/ci.yml']:
            with self.subTest(path=path):
                self.assertEqual(scan(path, manifest), [])
        self.assertEqual(scan('sylphx.toml', '[service]\norigin="http://api.tenant.svc.cluster.local"\n'), [])
        self.assertEqual(len(scan('apps/api/config.yaml', manifest)), 1)
        self.assertEqual(len(scan('src/infra/client.ts', 'const origin="http://api.tenant.svc.cluster.local";')), 1)
        self.assertEqual(scan('tests/client.ts', 'const origin="http://api.tenant.svc.cluster.local";'), [])


if __name__ == '__main__':
    unittest.main()
