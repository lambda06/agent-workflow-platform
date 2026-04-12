"""
backend.models.execution

This module defines the data models representing the runtime state, logs, and outputs 
of the agent workflow engine. It captures execution traces, caught errors, and final 
workflow results after an execution cycle.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ExecutionLog(BaseModel):
    """
    A runtime audit log representing a singular action, observation, or state transition 
    made by an agent. Crucial for tracing agent thought processes and tool execution over time.
    """
    log_id: UUID = Field(
        default_factory=uuid4, 
        description="Unique identifier for this specific log event."
    )
    task_id: UUID = Field(
        ..., 
        description="The UUID of the task this log was generated for."
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), 
        description="When this logged action took place."
    )
    message: str = Field(
        ..., 
        description="A descriptive string of the agent action, observation, or system event."
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, 
        description="Arbitrary context data relevant to the log (e.g., inputs, token usage, tool outputs)."
    )


class ExecutionError(BaseModel):
    """
    Captures detailed failure events during pipeline execution. Used for runtime diagnostics,
    error reporting, and enabling potential retry strategies in nodes.
    """
    error_id: UUID = Field(
        default_factory=uuid4, 
        description="Unique identifier for the error entry."
    )
    task_id: Optional[UUID] = Field(
        default=None, 
        description="The specific Task ID where the failure occurred, if applicable."
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), 
        description="Time the error was encountered."
    )
    error_type: str = Field(
        ..., 
        description="The class or category of the raised exception (e.g., 'TimeoutError', 'APIConnectionError')."
    )
    message: str = Field(
        ..., 
        description="The exception message or descriptive error text."
    )
    stack_trace: Optional[str] = Field(
        default=None, 
        description="The raw stack trace or log payload leading to the crash."
    )


class WorkflowResult(BaseModel):
    """
    Represents the final, aggregated outcome of a workflow execution.
    It encapsulates completed outputs, encountered errors, and specific completion states under one entity.
    """
    result_id: UUID = Field(
        default_factory=uuid4, 
        description="Persistent identifier for this summarized result."
    )
    workflow_id: UUID = Field(
        ..., 
        description="The initial workflow request ID that spawned this execution cycle."
    )
    status: str = Field(
        ..., 
        description="Final status reflecting process completion (e.g., 'SUCCESS', 'PARTIAL_SUCCESS', 'FAILED')."
    )
    final_output: Dict[str, Any] = Field(
        default_factory=dict, 
        description="The aggregated final outputs or artifacts produced collectively by the tasks."
    )
    logs: List[ExecutionLog] = Field(
        default_factory=list, 
        description="A subset or complete chronological trace compiled from sequential task completions."
    )
    errors: List[ExecutionError] = Field(
        default_factory=list, 
        description="Caught and reported errors spanning the entire workflow traversal."
    )
    completed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), 
        description="Timestamp marking when the workflow finished executing."
    )
