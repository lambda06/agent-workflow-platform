"""
backend.models.workflow

This module defines the core data models for defining workflows and task structures
before they are processed by the agent workflow engine. It structures inputs, tracking 
properties, and sub-components.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """
    Represents the valid execution statuses for an individual workflow task.
    """
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentType(str, Enum):
    """
    Defines the specialized agent types available within the workflow engine.
    """
    EXTRACTION = "extraction"
    TRANSFORM = "transform"
    INTEGRATION = "integration"
    NOTIFICATION = "notification"
    COORDINATION = "coordination"


class Task(BaseModel):
    """
    Represents a specific unit of work or instruction to be processed by a designated agent.
    """
    id: UUID = Field(
        default_factory=uuid4, 
        description="Unique identifier for the task."
    )
    description: str = Field(
        ..., 
        description="The main content or instruction for the task."
    )
    agent_type: AgentType = Field(
        ..., 
        description="The target agent equipped to handle this task."
    )
    status: TaskStatus = Field(
        default=TaskStatus.PENDING, 
        description="The current execution status of this task."
    )
    dependencies: List[UUID] = Field(
        default_factory=list, 
        description="List of Task UUIDs that must complete before this task can start."
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), 
        description="Timestamp marking task creation."
    )


class WorkflowRequest(BaseModel):
    """
    Represents the incoming workflow request and its underlying task structure.
    It encapsulates the orchestrated workflow sequence and initial entry requirements.
    """
    workflow_id: UUID = Field(
        default_factory=uuid4, 
        description="Unique workflow sequence identifier."
    )
    name: str = Field(
        ..., 
        description="A human-readable name describing the overarching goal of the workflow."
    )
    tasks: List[Task] = Field(
        ..., 
        description="Ordered or interdependent list of tasks to execute in this workload."
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), 
        description="Timestamp marking the reception of the workflow request."
    )
