"""
Task type definitions for the DPC Agent.

This module provides the data structures for defining custom task types
that the agent can register and execute.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List


@dataclass
class TaskTypeDefinition:
    """
    Defines a custom task type with execution instructions.

    When a task of this type is executed, the agent will follow the
    execution_prompt with task.data available as template variables.

    Attributes:
        task_type: Unique identifier (e.g., "weather_report")
        description: Human-readable description of what this task does
        execution_prompt: Instructions for the agent to follow when executing.
                         Can use {variable} placeholders that will be filled
                         from task.data
        input_schema: JSON schema for validating task.data (optional)
        created_at: When this task type was registered
        examples: Example task.data payloads (optional)

    Example:
        >>> definition = TaskTypeDefinition(
        ...     task_type="weather_report",
        ...     description="Fetch and summarize weather for a location",
        ...     execution_prompt="Fetch current weather for {location} and summarize it.",
        ...     input_schema={"type": "object", "properties": {"location": {"type": "string"}}},
        ...     created_at=datetime.utcnow(),
        ... )
    """

    task_type: str
    description: str
    execution_prompt: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    examples: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "task_type": self.task_type,
            "description": self.description,
            "execution_prompt": self.execution_prompt,
            "input_schema": self.input_schema,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "examples": self.examples,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskTypeDefinition":
        """Create from dictionary (e.g., loaded from JSON)."""
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.utcnow()

        return cls(
            task_type=data["task_type"],
            description=data["description"],
            execution_prompt=data["execution_prompt"],
            input_schema=data.get("input_schema", {}),
            created_at=created_at,
            examples=data.get("examples", []),
        )

    def format_prompt(self, task_data: Dict[str, Any]) -> str:
        """
        Format the execution_prompt with task_data variables.

        Args:
            task_data: The data payload from the task

        Returns:
            Formatted prompt string with variables substituted
        """
        try:
            return self.execution_prompt.format(**task_data)
        except KeyError as e:
            # If a placeholder is missing, provide a helpful error
            return f"{self.execution_prompt}\n\n[Warning: Missing variable {e} in task data]"


# Built-in task type definitions (for reference)
BUILTIN_TASK_TYPES = {
    "chat": TaskTypeDefinition(
        task_type="chat",
        description="Standard chat/conversation task",
        execution_prompt="{text}",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The message to process"},
                "dpc_context": {"type": "object", "description": "Optional DPC context"},
            },
            "required": ["text"],
        },
    ),
    "improvement": TaskTypeDefinition(
        task_type="improvement",
        description="Run an evolution/improvement cycle on the agent",
        execution_prompt="Run a self-improvement cycle.",
        input_schema={"type": "object"},
    ),
    "review": TaskTypeDefinition(
        task_type="review",
        description="Perform a code review task",
        execution_prompt="Review the following code or changes.",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "file_path": {"type": "string"},
            },
        },
    ),
    "reminder": TaskTypeDefinition(
        task_type="reminder",
        description="Deliver a reminder message directly to the user without LLM processing. Use this instead of 'chat' for notifications/alerts to avoid accidental re-scheduling loops.",
        execution_prompt="",  # Not used — reminder tasks bypass the LLM
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The reminder text to deliver"},
            },
            "required": ["message"],
        },
    ),
}
