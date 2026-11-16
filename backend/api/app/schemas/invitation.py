from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

InvitationRole = Literal["admin", "editor", "viewer"]


class InvitationCreateRequest(BaseModel):
    email: EmailStr
    role: InvitationRole = "viewer"

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("A valid email address is required.")

        return value.strip().lower()


class InvitationResponse(BaseModel):
    id: UUID
    organization_id: UUID
    email: EmailStr
    role: InvitationRole
    status: Literal["pending", "accepted", "revoked"]
    expires_at: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InvitationAcceptRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)
