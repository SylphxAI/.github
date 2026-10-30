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
