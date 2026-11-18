import tempfile
from pathlib import Path, PurePosixPath

from maf.schemas import GeneratedFile


def collect_python_files(generated: list[GeneratedFile]) -> dict[PurePosixPath, str]:
    files = {}
    for file in generated:
        path = PurePosixPath(file.path)
        if (
            path.is_absolute() or ".." in path.parts or "\\" in file.path
            or "\x00" in file.path or path.suffix != ".py" or path in files
        ):
            raise ValueError(f"Invalid or duplicate Python file: {file.path}")
        files[path] = file.content
    return files


def write_attempt(
    workspace: Path, run_id: str, spec_version: int, files: dict[PurePosixPath, str]
) -> Path:
    if not run_id or not run_id.isascii() or not run_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid run ID")
    if spec_version < 1:
        raise ValueError("Spec version must be positive")
    for path in files:
        if any(parent in files for parent in path.parents):
            raise ValueError(f"Python file used as a directory: {path}")
    parent = workspace / run_id / f"v{spec_version}"
    if parent.resolve() != parent:
        raise ValueError("Workspace path contains a symbolic link")
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))
    for relative_path, content in files.items():
        destination = directory / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    return directory
