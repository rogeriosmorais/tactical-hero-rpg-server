"""The standalone server owns its SQLite writer without a gateway process."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from game_server.profile_lock import own_profile, sidecar_path


ROOT = Path(__file__).resolve().parents[1]


class ServerWriterTests(unittest.TestCase):
    def test_second_process_cannot_take_server_writer_then_recovers_on_release(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'player.sqlite'
            snippet = ('from game_server.profile_lock import own_profile; '
                       'from pathlib import Path; '
                       'db = Path(' + repr(str(db)) + '); '
                       'lock = own_profile(db); '
                       'lock.__enter__(); lock.__exit__(None, None, None)')

            def probe():
                return subprocess.run([sys.executable, '-S', '-c', snippet],
                                      cwd=ROOT, capture_output=True, text=True, timeout=10)

            with own_profile(db):
                self.assertTrue(sidecar_path(db).exists())
                blocked = probe()
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn('Profile writer already owned', blocked.stderr)
            accepted = probe()
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertFalse(db.exists())  # Lock ownership never seeds a player.


if __name__ == '__main__':
    unittest.main()
