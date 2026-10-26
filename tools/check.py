"""Run the public server's own core tests; no private paths or dependencies."""
import ast
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    files = [*ROOT.joinpath('game_server').rglob('*.py'), *ROOT.joinpath('tests').rglob('*.py'), *ROOT.joinpath('tools').glob('*.py')]
    for path in files:
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT / 'tests')))
    print(json.dumps({'status': 'passed' if result.wasSuccessful() else 'failed', 'tests_run': result.testsRun, 'syntax_files': len(files)}))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
