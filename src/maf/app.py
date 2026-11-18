"""Build the project-scoped application graph."""

from pathlib import Path

from maf.graph import build_graph
from maf.model_team import ModelTeam
from maf.projects import Project, project_context
from maf.sandbox.docker_runner import DockerPytestRunner


def build_application_graph(root: Path, project: Project, checkpointer):
    return build_graph(
        ModelTeam.from_config(root),
        DockerPytestRunner(project.storage / "workspace"),
        checkpointer,
        lambda state: project_context(project),
    )
