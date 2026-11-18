import os
import subprocess
from collections import deque
from pathlib import Path, PurePosixPath
from threading import Thread
from uuid import uuid4

from maf.sandbox.runner import collect_python_files, write_attempt
from maf.schemas import CodeChange, TestResult, TestSuite


def is_test(path: PurePosixPath) -> bool:
    return path.name.startswith("test_") or path.name.endswith("_test.py")


class DockerPytestRunner:
    def __init__(self, workspace: Path, image: str = "maf-pytest:local", timeout: float = 60):
        self.workspace = workspace.resolve()
        self.image = image
        self.timeout = timeout

    def __call__(self, run_id: str, spec_version: int, code: CodeChange, tests: TestSuite, *, project_id: str | None = None):
        source_files = collect_python_files(code.files)
        qa_files = collect_python_files(tests.files)
        source = {
            path: body for path, body in source_files.items()
            if not is_test(path) and path.name != "conftest.py"
        }
        suite = {path: body for path, body in qa_files.items() if is_test(path)}
        ignored = sorted(map(str, (source_files.keys() - source.keys()) | (qa_files.keys() - suite.keys())))
        directory = write_attempt(self.workspace, run_id, spec_version, {**source, **suite})
        if not suite:
            return TestResult(
                exit_code=5, stdout="QA supplied no test_*.py or *_test.py files.",
                workspace=str(directory), ignored_files=ignored,
            )

        name = f"maf-pytest-{uuid4().hex}"
        command = [
            "docker", "run", "--rm", "--pull=never", "--name", name,
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=64",
            "--memory=256m", "--cpus=1", "--log-driver=none",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777",
            "--mount", f"type=bind,src={directory},dst=/work,readonly",
            "--workdir", "/work", "--env", "HOME=/tmp",
            "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint", "python",
            self.image, "-m", "pytest", "-c", "/dev/null", "-p", "no:cacheprovider",
            "-q", "--", *map(str, suite),
        ]
        exit_code, stdout = self.run_container(name, command)
        (directory / "pytest.log").write_text(stdout, encoding="utf-8")
        if exit_code in {125, 126, 127}:
            raise RuntimeError(f"Docker could not run pytest. See {directory / 'pytest.log'}")
        return TestResult(
            exit_code=exit_code, stdout=stdout, workspace=str(directory), ignored_files=ignored,
        )

    def run_container(self, name: str, command: list[str]) -> tuple[int, str]:
        chunks: deque[bytes] = deque(maxlen=4)

        def read_output(pipe):
            while chunk := pipe.read(4096):
                chunks.append(chunk)

        with subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        ) as process:
            reader = Thread(target=read_output, args=(process.stdout,))
            reader.start()
            try:
                exit_code = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                subprocess.run(
                    ["docker", "rm", "--force", name], capture_output=True, timeout=10, check=False,
                )
                exit_code = 124
            finally:
                reader.join()
        output = b"".join(chunks).decode("utf-8", errors="replace")
        if exit_code == 124:
            output += f"\nPytest timed out after {self.timeout:g} seconds."
        return exit_code, output
