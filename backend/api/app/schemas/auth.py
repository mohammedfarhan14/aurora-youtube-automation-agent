from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("Display name cannot be empty.")

        return value


class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str
    is_active: bool
    email_verified: bool = Field(
        validation_alias="email_verified_at",
    )

    model_config = ConfigDict(from_attributes=True)

    @field_validator("email_verified", mode="before")
    @classmethod
    def derive_email_verified(cls, value: object) -> bool:
        return value is not None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class BrowserSessionLoginResponse(BaseModel):
    user: UserResponse
    csrf_token: str


class EmailVerificationRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)
