"""Run each suite in a clean process, allowing loopback mocks only.

Usage: python -m tests.run_offline [tests.test_module ...]
No exchange credentials or production environment are inherited.
"""
import concurrent.futures
import ipaddress
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import sysconfig
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def install_network_guard():
    def audit(event, args):
        if event == 'socket.connect':
            address = args[1]
            if not isinstance(address, tuple):
                raise PermissionError('offline tests: non-IP socket blocked')
            host = address[0]
        elif event == 'socket.getaddrinfo':
            host = args[0]
        else:
            return
        if host == 'localhost':
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise PermissionError('offline tests: external network blocked')
    sys.addaudithook(audit)


def run_one(module):
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG') if k in os.environ}
    # -S prevents the repository's production sitecustomize from importing
    # trading modules before each suite can install its loopback mock and mode.
    # Add the active interpreter's dependencies explicitly because -S also
    # skips automatic site-packages discovery.
    child_path = os.pathsep.join((str(ROOT), sysconfig.get_paths()['purelib']))
    env.update(PYTHONPATH=child_path, PAPER_TRADE='true', LOG_LEVEL='ERROR',
               KUCOIN_REST_BASE='http://127.0.0.1:1', NEXUS_TELEGRAM='false')
    try:
        with tempfile.TemporaryDirectory(prefix='nexus-test-') as cwd:
            proc = subprocess.run(
                [sys.executable, '-S', '-m', 'tests.run_offline', '--child', module],
                cwd=cwd, env=env, capture_output=True, text=True, timeout=240,
            )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or '') + (exc.stderr or '')
        return module, 124, 0, output + f'\nTIMEOUT: {module} exceeded 240s\n'
    output = proc.stdout + proc.stderr
    count = re.findall(r'(?:PASSOU|PASSARAM|PASS|Passou):\s*(\d+)', output)
    unit = re.search(r'Ran (\d+) tests?', output)
    return module, proc.returncode, int(count[-1]) if count else (int(unit[1]) if unit else 0), output


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        module = sys.argv[2]
        sys.argv = [module]
        install_network_guard()
        loaded = __import__(module, fromlist=['*'])
        suite = unittest.defaultTestLoader.loadTestsFromModule(loaded)
        if suite.countTestCases():
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            sys.exit(not result.wasSuccessful())
        runpy.run_module(module, run_name='__main__', alter_sys=True)
    else:
        modules = sys.argv[1:] or ['tests.' + p.stem for p in sorted((ROOT/'tests').glob('test_*.py'))]
        failed = 0
        total = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for module, rc, count, output in pool.map(run_one, modules):
                print(f'{module}: {"PASS" if rc == 0 else "FAIL"} ({count})', flush=True)
                total += count
                if rc:
                    failed += 1
                    print(output, flush=True)
        print(f'TOTAL={total} FAILED_SUITES={failed}', flush=True)
        sys.exit(bool(failed))
