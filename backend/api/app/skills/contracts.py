"""Immutable metadata and typed schemas for skills and tools.

These contracts deliberately describe capabilities; they do not execute code.
Tool execution must be provided by separately authorized application services.
"""

import re
from dataclasses import dataclass

from pydantic import BaseModel

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase dotted identifier.")


def _validate_version(value: str) -> None:
    if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
        raise ValueError("Version must use canonical semantic versioning (MAJOR.MINOR.PATCH).")


def _validate_schema(schema: type[BaseModel], label: str) -> None:
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise TypeError(f"{label} must be a Pydantic model class.")
    if (
        schema.model_config.get("extra") != "forbid"
        or schema.model_config.get("strict") is not True
    ):
        raise ValueError(f"{label} must forbid extra fields and enable strict validation.")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A typed declaration of one bounded tool; no handler is stored here."""

    tool_id: str
    version: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]

    def __post_init__(self) -> None:
        _validate_identifier(self.tool_id, "Tool ID")
        _validate_version(self.version)
        if not self.description.strip():
            raise ValueError("Tool description cannot be empty.")
        _validate_schema(self.input_schema, "Tool input schema")
        _validate_schema(self.output_schema, "Tool output schema")

    def validate_input(self, payload: object) -> BaseModel:
        return self.input_schema.model_validate(payload, strict=True)

    def validate_output(self, payload: object) -> BaseModel:
        return self.output_schema.model_validate(payload, strict=True)


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    """An immutable, version-addressed skill contract."""

    skill_id: str
    version: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    allowed_tools: frozenset[str] = frozenset()
    model_capabilities: tuple[str, ...] = ()
    guardrails: tuple[str, ...] = ()
    evaluation_case_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.skill_id, "Skill ID")
        _validate_version(self.version)
        if not self.description.strip():
            raise ValueError("Skill description cannot be empty.")
        _validate_schema(self.input_schema, "Skill input schema")
        _validate_schema(self.output_schema, "Skill output schema")

        if not isinstance(self.allowed_tools, frozenset):
            raise TypeError("Allowed tools must be a frozenset.")
        for tool_reference in self.allowed_tools:
            if not isinstance(tool_reference, str) or "@" not in tool_reference:
                raise ValueError("Allowed tools must use exact tool_id@version references.")
            tool_id, separator, tool_version = tool_reference.rpartition("@")
            _validate_identifier(tool_id, "Allowed tool ID")
            if not separator:
                raise ValueError("Allowed tools must use exact tool_id@version references.")
            _validate_version(tool_version)

        for field_name, values in (
            ("model capabilities", self.model_capabilities),
            ("guardrails", self.guardrails),
            ("evaluation case IDs", self.evaluation_case_ids),
        ):
            if not isinstance(values, tuple):
                raise TypeError(f"Skill {field_name} must be a tuple.")
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"Skill {field_name} cannot contain empty values.")

    def validate_input(self, payload: object) -> BaseModel:
        """Validate untrusted caller input against this exact skill version."""
        return self.input_schema.model_validate(payload, strict=True)

    def validate_output(self, payload: object) -> BaseModel:
        """Validate generated output before it becomes a persisted artifact."""
        return self.output_schema.model_validate(payload, strict=True)
