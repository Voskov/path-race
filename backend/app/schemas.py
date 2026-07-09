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


class TapEditIn(BaseModel):
    id: str                       # must belong to the trip being edited
    client_ts: Optional[int] = None   # new timestamp (ms epoch); ignored if delete
    delete: bool = False


class TripEditIn(BaseModel):
    """Atomic commit from the stats-page trip editor. Only fields present are
    changed; taps not listed keep their stored timestamps. An active trip
    cannot be edited (the phone owns it)."""
    status: Optional[Literal["done", "discarded"]] = None
    crowding: Optional[int] = Field(default=None, ge=1, le=3)
    anomalous: Optional[bool] = None
    anomaly_reason: Optional[str] = None  # "" clears the reason
    taps: list[TapEditIn] = Field(default_factory=list)
