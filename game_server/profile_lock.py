"""Exclusive process-lifetime ownership of an application's SQLite database.

Moved from the retired local gameplay writer to the standalone server. Only
the application process acquires this file lock; the protocol gateway never
owns game state or database handles.
"""
from contextlib import contextmanager
import os
from pathlib import Path


def sidecar_path(database):
    return Path(str(Path(database).resolve()) + '.runtime-writer.lock')


@contextmanager
def own_profile(database):
    path = sidecar_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('Profile writer already owned: ' + str(path)) from error
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
