from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class StructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Plan(StructuredOutput):
    goal: str
    requirements: list[str]
    acceptance_criteria: list[str]
    out_of_scope: list[str]


class IntakePlan(StructuredOutput):
    action: Literal["plan"]
    plan: Plan


class IntakeReply(StructuredOutput):
    action: Literal["reply", "clarify"]
    message: str = Field(min_length=1, max_length=1500, pattern=r"\S")


class IntakeDecision(StructuredOutput):
    outcome: Annotated[IntakePlan | IntakeReply, Field(discriminator="action")]


class PlannedFile(StructuredOutput):
    path: str
    purpose: str


class PublicAPI(StructuredOutput):
    signature: str
    behavior: str


class TestCase(StructuredOutput):
    name: str
    given: str
    expect: str


class Design(StructuredOutput):
    files: list[PlannedFile]
    public_api: list[PublicAPI]
    data_models: list[str]
    test_cases: list[TestCase]
    assumptions: list[str]


class GeneratedFile(StructuredOutput):
    path: str
    content: str


class CodeChange(StructuredOutput):
    files: list[GeneratedFile]
    notes: str


class TestSuite(StructuredOutput):
    files: list[GeneratedFile]


class TestResult(StructuredOutput):
    exit_code: int | None
    stdout: str
    workspace: str | None = None
    ignored_files: list[str] = Field(default_factory=list)


class Report(StructuredOutput):
    summary: str
    files: list[str]
    test_result: TestResult
    open_issues: list[str]
