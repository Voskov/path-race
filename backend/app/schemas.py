"""Request/response models."""
from typing import Literal, Optional
from pydantic import BaseModel, Field


class TapIn(BaseModel):
    id: str                       # client-generated, idempotency key
    checkpoint_key: str
    client_ts: int                # device Date.now() at commit
    seq: int
    lat: Optional[float] = None
    lng: Optional[float] = None
    accuracy: Optional[float] = None


class CreateTripIn(BaseModel):
    id: str                       # client-generated trip uuid
    first_tap: TapIn              # first tap fixes direction (home|office)


class TapsBatchIn(BaseModel):
    taps: list[TapIn] = Field(default_factory=list)


class PatchTripIn(BaseModel):
    status: Optional[Literal["active", "done", "discarded"]] = None
    crowding: Optional[int] = Field(default=None, ge=1, le=3)
    anomalous: Optional[bool] = None
    anomaly_reason: Optional[str] = None
    completed_at: Optional[int] = None
