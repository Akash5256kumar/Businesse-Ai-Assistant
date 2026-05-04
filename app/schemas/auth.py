from __future__ import annotations

from pydantic import BaseModel, field_validator


class SendOTPRequest(BaseModel):
    email: str | None = None
    phone_number: str | None = None
    purpose: str = "login"


class SendOTPResponse(BaseModel):
    message: str
    destination: str
    expires_in_seconds: int
    debug_otp: str | None = None


class VerifyOTPRequest(BaseModel):
    email: str | None = None
    phone_number: str | None = None
    otp: str
    purpose: str = "login"


class VerifyOTPResponse(BaseModel):
    message: str
    verified: bool
    access_token: str
    token_type: str
    user_id: int
    is_new_user: bool


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_new_user: bool
    user_id: int


class BusinessSetupRequest(BaseModel):
    business_name: str
    owner_name: str
    category: str | None = None


class BusinessSetupResponse(BaseModel):
    message: str
    business_id: int
    business_name: str


class ProfileSetupRequest(BaseModel):
    full_name: str
    user_type: str = "business"   # "business" | "customer"
    business_name: str | None = None  # required when user_type == "business"
    location: str | None = None       # optional, for business users

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("must be at least 2 characters")
        if len(v) > 100:
            raise ValueError("must be at most 100 characters")
        return v

    @field_validator("user_type")
    @classmethod
    def validate_user_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("business", "customer"):
            raise ValueError("user_type must be 'business' or 'customer'")
        return v

    @field_validator("business_name")
    @classmethod
    def validate_business_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if len(v) < 2:
            raise ValueError("must be at least 2 characters")
        if len(v) > 100:
            raise ValueError("must be at most 100 characters")
        return v


class ProfileSetupResponse(BaseModel):
    message: str
    user_id: int
    full_name: str
    user_type: str
    business_id: int | None = None
    business_name: str | None = None
    location: str | None = None
