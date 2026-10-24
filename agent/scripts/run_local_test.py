"""Run one explicit unittest module without credentials, network or production data."""
import os
import socket
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path


def run_worker(module, directory):
    # Set these before importing config or any application package.
    os.environ['EQUIS_ENV_FILE'] = ''
    os.environ['BAILIAN_API_KEY'] = 'offline-test-key'
    os.environ['ANONYMIZED_TELEMETRY'] = 'False'
    def offline(*args, **kwargs):
        raise OSError('Local test runner blocks outbound network access')
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    socket.create_connection = offline
    os.environ['EQUIS_DATA_DIR'] = directory
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    trace = sys.modules.get('observability.trace')
    if trace is not None:
        trace._close_conn()
    return 0 if result.wasSuccessful() else 1


def main(module):
    # Chroma and background SQLite workers can retain native file handles.
    # Let the worker exit completely before the parent removes its temporary data.
    with tempfile.TemporaryDirectory(prefix='equis_local_test_') as directory:
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(root.parent), str(root), os.environ.get('PYTHONPATH', ''))),
               'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-B', '-m', 'scripts.run_local_test',
                                 '--worker', module, directory], cwd=root, env=env)
        return result.returncode


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        raise SystemExit(run_worker(sys.argv[2], sys.argv[3]))
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python -m scripts.run_local_test <test module>')
    raise SystemExit(main(sys.argv[1]))
