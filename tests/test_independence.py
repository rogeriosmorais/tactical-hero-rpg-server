import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IndependenceTests(unittest.TestCase):
    def test_public_tree_runs_with_no_siblings_site_packages_keys_or_captures(self):
        with tempfile.TemporaryDirectory(prefix='independent-public-server-') as directory:
            isolated = Path(directory)
            for folder in ('game_server', 'tools'):
                shutil.copytree(ROOT / folder, isolated / folder, ignore=shutil.ignore_patterns('__pycache__'))
            (isolated / 'tests').mkdir()
            for test in (ROOT / 'tests').glob('test_*.py'):
                if test.name != Path(__file__).name:
                    shutil.copy2(test, isolated / 'tests' / test.name)
            process = subprocess.run([sys.executable, '-I', '-S', '-B', str(isolated / 'tools/isolated_probe.py'), '--core-tests'],
                                     cwd=isolated, capture_output=True, text=True, timeout=60)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result['status'], 'passed')
            self.assertFalse(result['external_site_packages_enabled'])
            self.assertTrue(result['http_started'])
            self.assertGreater(result['core_tests'], 0)

    def test_runtime_source_has_no_external_checkout_or_client_dependencies(self):
        import ast
        imports = set()
        for path in (ROOT / 'game_server').glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    imports.add(node.module.split('.')[0])
                elif isinstance(node, ast.Import):
                    imports.update(alias.name.split('.')[0] for alias in node.names)
        self.assertTrue(imports <= sys.stdlib_module_names, str(imports - sys.stdlib_module_names))
        for name in ('research', 'archives', 'assets', 'backend', 'msf_workspace.py'):
            self.assertFalse((ROOT / name).exists(), name)
