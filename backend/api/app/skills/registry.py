"""In-memory registry for explicitly installed, immutable skill/tool versions."""

from collections.abc import Iterable

from app.skills.contracts import SkillDefinition, ToolDefinition


class UnknownSkillError(LookupError):
    """Raised when an exact skill ID/version is not registered."""


class UnknownToolError(LookupError):
    """Raised when a skill declares a tool that is not registered."""


class SkillRegistry:
    """Resolve exact versions and enforce tool allowlists at registration time.

    There is intentionally no implicit "latest" lookup: workflow records should
    persist the exact version they executed so a later registry update cannot
    silently change an existing workflow's behavior.
    """

    def __init__(
        self,
        *,
        tools: Iterable[ToolDefinition] = (),
        skills: Iterable[SkillDefinition] = (),
    ) -> None:
        self._tools: dict[tuple[str, str], ToolDefinition] = {}
        self._skills: dict[tuple[str, str], SkillDefinition] = {}

        for tool in tools:
            self.register_tool(tool)
        for skill in skills:
            self.register_skill(skill)

    def register_tool(self, tool: ToolDefinition) -> None:
        if not isinstance(tool, ToolDefinition):
            raise TypeError("Only ToolDefinition instances can be registered.")
        key = (tool.tool_id, tool.version)
        if key in self._tools:
            raise ValueError(f"Tool version is already registered: {tool.tool_id}@{tool.version}")
        self._tools[key] = tool

    def register_skill(self, skill: SkillDefinition) -> None:
        if not isinstance(skill, SkillDefinition):
            raise TypeError("Only SkillDefinition instances can be registered.")
        key = (skill.skill_id, skill.version)
        if key in self._skills:
            raise ValueError(
                f"Skill version is already registered: {skill.skill_id}@{skill.version}"
            )

        registered_tools = {f"{tool_id}@{version}" for tool_id, version in self._tools}
        unknown_tools = skill.allowed_tools - registered_tools
        if unknown_tools:
            raise UnknownToolError(
                "Skill declares unregistered tool(s): " + ", ".join(sorted(unknown_tools))
            )
        self._skills[key] = skill

    def get_skill(self, skill_id: str, version: str) -> SkillDefinition:
        try:
            return self._skills[(skill_id, version)]
        except KeyError:
            raise UnknownSkillError(f"Unknown skill version: {skill_id}@{version}") from None

    def get_tool(self, tool_id: str, version: str) -> ToolDefinition:
        try:
            return self._tools[(tool_id, version)]
        except KeyError:
            raise UnknownToolError(f"Unknown tool version: {tool_id}@{version}") from None

    def tools_for_skill(self, skill_id: str, version: str) -> tuple[ToolDefinition, ...]:
        skill = self.get_skill(skill_id, version)
        return tuple(
            self._tools[key]
            for key in sorted(self._tools)
            if f"{key[0]}@{key[1]}" in skill.allowed_tools
        )

    def list_skills(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._skills[key] for key in sorted(self._skills))

    def list_tools(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._tools[key] for key in sorted(self._tools))
