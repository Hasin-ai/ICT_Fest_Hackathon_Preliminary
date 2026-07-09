"""Pydantic request/response models."""
from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    org_name: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginRequest(BaseModel):
    org_name: str
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class RoomCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    capacity: int = Field(gt=0)
    hourly_rate_cents: int = Field(gt=0)


class BookingCreateRequest(BaseModel):
    room_id: int
    start_time: str
    end_time: str
