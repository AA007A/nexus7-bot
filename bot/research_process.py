"""Bounded research subprocess; no production credentials or network access."""
import asyncio
import json
import os
from pathlib import Path
import sys
import sysconfig


async def run_snapshot(snapshot, timeout=1800):
    payload = json.dumps(snapshot, allow_nan=False).encode()
    root = str(Path(__file__).resolve().parents[1])
    env = {k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ}
    dependencies = [p for p in sys.path if p and ('site-packages' in p or 'dist-packages' in p)]
    env.update(PYTHONPATH=os.pathsep.join(dict.fromkeys([root, *dependencies, sysconfig.get_paths()['purelib']])),
        PAPER_TRADE='true', NEXUS_TELEGRAM='false', LOG_LEVEL='ERROR',
        OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
    proc = await asyncio.create_subprocess_exec(sys.executable, '-S', '-m',
        'bot.research_process', cwd=root, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(payload), timeout)
        if proc.returncode != 0:
            raise RuntimeError('isolated research failed')
        result = json.loads(stdout)
        if not isinstance(result, dict) or not isinstance(result.get('metadata'), dict):
            raise ValueError('invalid research response')
        return result
    finally:
        # communicate() already waits for normal process termination. On timeout or
        # cancellation, terminate only if the child is still running. ProcessLookupError
        # is an expected race (the child exited between returncode check and kill), not
        # a silent failure worth flagging by the safety audit.
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            finally:
                await proc.wait()


def main():
    def deny_network(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.bind'):
            raise PermissionError('research network disabled')
    sys.addaudithook(deny_network)
    from contextlib import redirect_stdout
    snapshot = json.load(sys.stdin)
    with redirect_stdout(sys.stderr):
        from bot.optimizer import optimize_snapshot
        result = optimize_snapshot(**snapshot)
    json.dump(result, sys.stdout, allow_nan=False)


if __name__ == '__main__':
    main()
