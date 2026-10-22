"""Compatibility and composite-consumer tests for the shared brand generator."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import yaml

ACTION = Path(__file__).resolve().parents[1] / '.github/actions/brand'


def fixture(root):
    brand = root / 'brand data'
    brand.mkdir()
    (brand / 'brand.json').write_text(json.dumps({'outputs': {'public/master.svg': 'master.svg'}}))
    (brand / 'tokens.json').write_text('{"color": {"ink": {"$value": "#123456"}}}')
    (brand / 'master.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>\n')
    (root / 'public').mkdir()
    (root / 'public/master.svg').write_bytes((brand / 'master.svg').read_bytes())
    provenance(brand)
    return brand


def provenance(brand):
    files = {p.name: {'source': 'fixture', 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in brand.iterdir() if p.name != 'provenance.json'}
    (brand / 'provenance.json').write_text(json.dumps({'files': files}))


class BrandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.brand = fixture(self.root)

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(ACTION / 'build.py'),
                               '--brand-dir', str(self.brand), *args],
                              cwd=self.root, capture_output=True, text=True)

    def test_check_is_read_only_and_stdlib(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.run_script('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_corrupt_missing_unlisted_and_surface_files_fail(self):
        cases = ('corrupt', 'missing', 'unlisted', 'surface')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                self.root = Path(tmp)
                self.brand = fixture(self.root)
                if case == 'corrupt':
                    (self.brand / 'master.svg').write_text('changed')
                elif case == 'missing':
                    (self.brand / 'master.svg').unlink()
                elif case == 'unlisted':
                    (self.brand / 'extra.txt').write_text('unrecorded')
                else:
                    (self.root / 'public/master.svg').write_text('wrong copy')
                self.assertNotEqual(self.run_script('--check').returncode, 0)

    def test_write_preserves_source_metadata_and_repairs_surfaces(self):
        (self.root / 'public/master.svg').unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.run_script('--check').returncode, 0)
        prov = json.loads((self.brand / 'provenance.json').read_text())
        self.assertEqual(prov['files']['master.svg']['source'], 'fixture')
        self.assertIn('--brand-color-ink: #123456;', (self.brand / 'tokens.css').read_text())

    def test_explicit_surface_root(self):
        surface_root = self.root / 'other root'
        result = self.run_script('--root', str(surface_root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_script('--root', str(surface_root), '--check').returncode, 0)

    def test_invalid_arguments_fail(self):
        self.assertNotEqual(self.run_script('--render-fit', 'unknown').returncode, 0)
        self.assertNotEqual(self.run_script('--check', '--resnap').returncode, 0)

    def load_script(self, *args):
        with patch.object(sys, 'argv', ['build.py', '--brand-dir', str(self.brand), *args]):
            return runpy.run_path(str(ACTION / 'build.py'))

    def test_render_fit_preserves_both_existing_behaviours(self):
        image = Mock(size=(80, 40), width=80, height=40)
        image.convert.return_value = image
        canvas = Mock()
        pil = SimpleNamespace(Image=SimpleNamespace(open=Mock(return_value=image),
                                                   new=Mock(return_value=canvas), LANCZOS=1))
        renderer = SimpleNamespace(svg_to_bytes=Mock(return_value=b'png'))
        with patch.dict(sys.modules, {'PIL': pil, 'resvg_py': renderer}):
            self.assertIs(self.load_script()['render']('master.svg', 100), canvas)
            canvas.paste.assert_called_once_with(image, (10, 30))
            image.thumbnail.assert_called_once_with((100, 100), 1)
            image.thumbnail.reset_mock()
            self.assertIs(self.load_script('--render-fit', 'intrinsic')['render']('master.svg', 100), image)
            image.thumbnail.assert_not_called()

    def test_resnap_flag_reaches_build(self):
        namespace = self.load_script('--resnap')
        self.assertTrue(namespace['args'].resnap)
        function = namespace['build_icons']
        fake = Mock()
        functions = {name: Mock() for name in ('write_grid', 'snap', 'save_png', 'write_ico')}
        functions.update(read_grid=Mock(return_value=([], {})), grid_png=Mock(return_value=fake),
                         grid_svg=Mock(return_value='<svg/>'), render=Mock(return_value=fake))
        functions['write_grid'].side_effect = lambda size, rows, palette: (self.brand / f'favicon/grid-{size}.txt').write_text('fixture')
        with patch.dict(function.__globals__, functions):
            function({'master': 'master.svg', 'sizes': []}, True)
            self.assertEqual(functions['snap'].call_count, 2)
            functions['snap'].reset_mock()
            function({'master': 'master.svg', 'sizes': []}, False)
            functions['snap'].assert_not_called()

    def test_composite_in_both_modes_and_rejects_invalid_inputs(self):
        action = yaml.safe_load((ACTION / 'action.yml').read_text())
        shell = action['runs']['steps'][0]['run']
        import os
        env = dict(os.environ, GITHUB_ACTION_PATH=str(ACTION), BRAND_DIRECTORY=str(self.brand),
                   ROOT_DIRECTORY=str(self.root), RENDER_FIT='canvas', RESNAP='false')
        for mode in ('check', 'write', 'check'):
            result = subprocess.run(['bash', '-c', shell], cwd=self.root,
                                    env=dict(env, BRAND_MODE=mode), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for changes in ({'BRAND_MODE': 'invalid'}, {'BRAND_MODE': 'check', 'RESNAP': 'true'},
                        {'BRAND_MODE': 'check', 'RESNAP': 'invalid'}):
            result = subprocess.run(['bash', '-c', shell], cwd=self.root,
                                    env=dict(env, **changes), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
