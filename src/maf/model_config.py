from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from maf.model_backends import Backend, OpenAICompatible


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class APISettings(Settings):
    kind: Literal["openai_compatible"]
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    timeout: float = Field(default=60, gt=0)

    def build(self, root: Path) -> Backend:
        return OpenAICompatible(self.base_url, self.api_key_env, self.timeout)


class Route(Settings):
    backend: Literal["nine_router"]
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None


class Roles(Settings):
    supervisor: list[Route] = Field(min_length=1)
    architect: list[Route] = Field(min_length=1)
    developer: list[Route] = Field(min_length=1)
    qa: list[Route] = Field(min_length=1)


RoleName = Literal["supervisor", "architect", "developer", "qa"]
ToolName = Literal["list_runs", "get_run", "create_run", "get_project", "change_run", "control_run", "list_files", "read_file", "write_file", "command"]
PM_TOOLS = ("list_runs", "get_run", "create_run", "get_project", "change_run", "control_run")
WORK_TOOLS = ("list_files", "read_file", "write_file", "command")


class Skill(Settings):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    instructions: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
    enabled: bool = True


class AgentProfile(Settings):
    rules: str = Field(default="", max_length=8000)
    skills: list[Skill] = Field(default_factory=list, max_length=10)
    tools: set[ToolName] = Field(default_factory=set)

    @model_validator(mode="after")
    def unique_skill_names(self):
        if len({skill.name.casefold() for skill in self.skills}) != len(self.skills):
            raise ValueError("Skill names must be unique within the agent")
        return self


class AgentSettings(AgentProfile):
    routes: list[Route] = Field(min_length=1, max_length=10)


class ModelConfig(Settings):
    backends: dict[Literal["nine_router"], APISettings]
    roles: Roles
    agents: dict[RoleName, AgentProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_routes(self):
        for role, routes in self.roles:
            for route in routes:
                backend = self.backends.get(route.backend)
                if backend is None:
                    raise ValueError(f"{role} references unknown backend {route.backend}")
                if isinstance(backend, APISettings) and route.model is None:
                    raise ValueError(f"{role} must specify a model for {route.backend}")
        for role, profile in self.agents.items():
            allowed = set(PM_TOOLS if role == 'supervisor' else WORK_TOOLS)
            if not profile.tools <= allowed:
                raise ValueError(f'Unsupported tools for {role}')
        return self

    def profile(self, role: RoleName) -> AgentProfile:
        if role in self.agents:
            return self.agents[role]
        return AgentProfile(tools=set(PM_TOOLS if role == "supervisor" else WORK_TOOLS))

    @classmethod
    def save_agent(cls, root: Path, role: RoleName, settings: AgentSettings):
        path = root / "config/models.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw["roles"][role] = [route.model_dump(exclude_none=True) for route in settings.routes]
        profile = settings.model_dump(exclude={"routes"}, mode="json")
        profile["tools"] = sorted(profile["tools"])
        raw.setdefault("agents", {})[role] = profile
        updated = cls.model_validate(raw)
        with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            try:
                yaml.safe_dump(raw, temporary, allow_unicode=True, sort_keys=False)
                temporary.flush()
                temporary_path.replace(path)
            finally:
                temporary_path.unlink(missing_ok=True)
        return updated

    @classmethod
    def load(cls, root: Path):
        load_dotenv(root / ".env")
        return cls.model_validate(yaml.safe_load((root / "config/models.yaml").read_text(encoding="utf-8")))
