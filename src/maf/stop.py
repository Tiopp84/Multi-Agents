"""Stop development services belonging to this workspace."""

import os
import signal
import subprocess
import time
from pathlib import Path


def project_processes(root: Path):
    processes = set()
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if (entry / 'cwd').resolve() != root:
                continue
            args = (entry / 'cmdline').read_bytes().decode().split('\0')
            gateway = 'maf.serve' in args or 'maf.gateway:create_app' in args
            studio = 'dev' in args and any(Path(arg).name == 'langgraph' for arg in args)
            if gateway or studio:
                processes.add(int(entry.name))
        except (OSError, UnicodeError):
            continue
    return processes


def main():
    root = Path(__file__).resolve().parents[2]
    for unit in ('maf-dev', 'maf-gateway'):
        try:
            result = subprocess.run(
                ['systemctl', '--user', 'show', unit, '--property=WorkingDirectory', '--value'],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip() == str(root):
                subprocess.run(['systemctl', '--user', 'stop', unit], check=True, timeout=25)
        except FileNotFoundError:
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            print(f'Không dừng được service {unit}: {error}')
            return 1
    processes = project_processes(root)
    for pid in processes:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 20
    while project_processes(root) and time.monotonic() < deadline:
        time.sleep(0.2)
    remaining = project_processes(root)
    if remaining:
        print('Tiến trình chưa dừng: ' + ', '.join(map(str, sorted(remaining))))
        return 1
    print('Đã dừng Gateway và LangGraph Studio của dự án.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
