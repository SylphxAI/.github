"""Generated subtrees may be omitted only by a byte-exact trusted Git-tree proof."""
import hashlib
import json
import unittest

import test_source_relocations as relocation


class ProjectionTests(relocation.RelocationTests):
    # Inherited controls also verify unchanged full-tree registrations and hard guards.
    def projection(self):
        self.write('legacy/hk/server/vendor/engine/source.rs', 'generated source\n')
        self.write('legacy/hk/server/vendor/engine/linkname', 'generated metadata\n')
        (self.root / 'legacy/hk/server/vendor/engine/symlink').symlink_to('source.rs')
        self.git('update-index', '--chmod=+x', 'legacy/hk/db/0014_historical.sql')
        self.commit()
        self.record['source_tree'] = self.git('rev-parse', 'HEAD:legacy/hk')
        omission = {'path': 'server/vendor/engine',
                    'tree': self.git('rev-parse', 'HEAD:legacy/hk/server/vendor/engine'),
                    'dependency_repository': 'test/engine', 'dependency_commit': 'b' * 40,
                    'dependency_manifest': 'history/engine.json',
                    'dependency_manifest_sha256': hashlib.sha256(b'precise generator proof\n').hexdigest()}
        self.git('rm', '-qr', 'legacy/hk/server/vendor/engine')
        self.write('history/engine.json', 'precise generator proof\n')
        self.commit()
        self.record['generated_omissions'] = [omission]
        self.save_policy([self.record])
        self.projection_anchor = self.git('rev-parse', 'HEAD')
        return omission

    def test_exact_projection_is_visible_inherited_and_not_new_sql(self):
        self.projection()
        self.assertEqual('exact-origin-projection', self.records()[0]['proof'])
        report, errors = self.scoped_report()
        self.assertEqual([], errors)
        self.assertTrue(report['findings'])
        self.assertTrue(all(not row['changed'] for row in report['findings']))

    def test_modified_maintained_bytes_still_refused(self):
        self.projection()
        self.write('legacy/hk/server/package.json', '{"dependencies":{"hono":"999"}}')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'differs from recorded origin tree'):
            self.records()

    def test_wrong_generated_subtree_hash_refused(self):
        omitted = self.projection()
        omitted['tree'] = 'c' * 40
        self.save_policy([self.record])
        with self.assertRaisesRegex(ValueError, 'differs from recorded origin tree'):
            self.records()

    def test_regenerated_copy_is_not_an_omission(self):
        self.projection()
        self.write('legacy/hk/server/vendor/engine/source.rs', 'generated source\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'omission is present'):
            self.records()

    def test_source_root_file_mode_still_exact(self):
        self.projection()
        self.git('update-index', '--chmod=+x', 'legacy/hk/server/package.json')
        self.git('commit', '-qm', 'mode change')
        with self.assertRaisesRegex(ValueError, 'differs from recorded origin tree'):
            self.records()

    def test_generated_proof_manifest_change_refused(self):
        self.projection()
        self.write('history/engine.json', 'different source pin or generator\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'manifest differs'):
            self.records()

    def test_missing_dependency_proof_refused(self):
        self.projection()
        self.git('rm', 'history/engine.json')
        self.commit()
        with self.assertRaises(Exception):
            self.records()

    def test_overlapping_generated_paths_refused(self):
        omitted = self.projection()
        self.record['generated_omissions'].append({**omitted, 'path': 'server/vendor/engine/subdir'})
        self.save_policy([self.record])
        with self.assertRaisesRegex(ValueError, 'overlap'):
            self.records()

    def test_arbitrary_caller_omission_gets_no_inheritance(self):
        self.projection()
        del self.record['generated_omissions']
        self.save_policy([self.record])
        self.write('.github/source-provenance.json', json.dumps({'version': 1, 'origins': [self.record],
                   'generated_omissions': [{'path': 'server/vendor/engine'}]}))
        self.commit()
        with self.assertRaisesRegex(ValueError, 'differs from recorded origin tree'):
            self.records()

    def test_omission_ancestor_cannot_replace_file(self):
        self.projection()
        self.write('legacy/hk/server/vendor', 'not a directory')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'ancestor is not a tree'):
            self.records()

    def test_generated_path_traversal_refused(self):
        omitted = self.projection()
        omitted['path'] = '../escape'
        self.save_policy([self.record])
        with self.assertRaisesRegex(ValueError, 'invalid source-relocation prefix'):
            self.records()


if __name__ == '__main__':
    unittest.main()
