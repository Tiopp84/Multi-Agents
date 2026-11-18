"""Project registration, storage location and bounded input snapshots."""

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

METADATA = ".maf"
SKIP_NAMES = {"secrets.py", "credentials.py", "conftest.py"}
MAX_FILES = 40
MAX_FILE_BYTES = 12_000
MAX_TOTAL_BYTES = 80_000


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    workspace: Path
    brief: str

    @property
    def storage(self):
        path = self.workspace / METADATA
        if (path.is_symlink() or (path / "workspace").is_symlink()
                or self.workspace.resolve() != self.workspace or not path.is_dir()):
            raise ValueError("Project workspace or .maf directory is unavailable")
        return path

    def describe(self):
        return dict(id=self.id, name=self.name, workspace=str(self.workspace), brief=self.brief)


def create_project(connection, workspace: Path, name: str, brief: str):
    project = Project(uuid4().hex, name, workspace, brief)
    path = workspace / METADATA
    path.mkdir()
    marker = path / "project.json"
    try:
        marker.write_text(json.dumps(project.describe(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with connection:
            connection.execute(
                "INSERT INTO projects(id, name, workspace, brief) VALUES (?, ?, ?, ?)",
                (project.id, name, str(workspace), brief),
            )
    except Exception:
        marker.unlink(missing_ok=True)
        path.rmdir()
        raise
    return project


def workspace_files(directory: Path, limit=MAX_FILES):
    results = {}
    remaining = MAX_TOTAL_BYTES
    walked = 0
    for parent, dirs, names in os.walk(directory, followlinks=False):
        walked += 1
        if walked > 160:
            break
        dirs[:] = sorted(
            name for name in dirs if not name.startswith(".")
            and name not in {"node_modules", "venv", "__pycache__", "build", "dist"}
            and not (Path(parent) / name).is_symlink()
        )
        for name in sorted(names):
            if name.startswith(".") or name in SKIP_NAMES:
                continue
            file = Path(parent) / name
            if file.suffix.lower() not in {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sql", ".md", ".json", ".yaml", ".yml", ".txt"} or name in {"package-lock.json", "yarn.lock"}:
                continue
            if file.is_symlink() or not file.is_file():
                continue
            try:
                with file.open("rb") as stream:
                    contents = stream.read(MAX_FILE_BYTES + 1)
                if len(contents) <= min(MAX_FILE_BYTES, remaining):
                    results[file.relative_to(directory).as_posix()] = contents.decode("utf-8")
                    remaining -= len(contents)
            except (OSError, UnicodeError):
                continue
            if len(results) >= limit or remaining <= 0:
                return results
    return results


def project_history(project: Project):
    database = project.storage / "events.sqlite"
    if not database.is_file():
        return []
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute("""
            SELECT e.run_id, e.spec_version, e.type, e.payload_json, (
                SELECT json_extract(r.payload_json, '$.request') FROM events AS r
                WHERE r.run_id=e.run_id AND r.id<=e.id AND r.type IN ('run.created', 'spec.updated')
                ORDER BY r.id DESC LIMIT 1
            )
            FROM events AS e WHERE e.type IN ('run.finished', 'approval.requested', 'error')
            ORDER BY e.id DESC LIMIT 8
        """).fetchall()
    history = []
    for run_id, version, kind, raw, request in rows:
        value = json.loads(raw)
        report = value.get("report") or {}
        history.append(dict(run_id=run_id, spec_version=version, event=kind,
                            request=(request or "")[:500],
                            status=value.get("status"), gate=value.get("gate"),
                            summary=(report.get("summary") or value.get("supervisor_reply") or "")[:600]))
    return history


def project_baseline(project: Project):
    database = project.storage / "events.sqlite"
    if not database.is_file():
        return None, None
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute("""
            SELECT run_id, json_extract(payload_json, '$.report.test_result.workspace')
            FROM events WHERE type='run.finished'
                AND json_extract(payload_json, '$.report.test_result.exit_code') = 0
            ORDER BY id DESC LIMIT 1
        """).fetchone()
    return row or (None, None)


def project_context(project: Project):
    base_run, attempt = project_baseline(project)
    files = workspace_files(project.workspace)
    if attempt:
        directory = Path(attempt)
        if directory.resolve().is_relative_to(project.storage / "workspace") and directory.is_dir():
            files.update(workspace_files(directory))
    return dict(project_id=project.id, name=project.name, brief=project.brief, files=files,
                baseline_run_id=base_run, recent_progress=project_history(project))
