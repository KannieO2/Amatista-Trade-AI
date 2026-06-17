from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel, Field


class SignalOutcome(StrEnum):
    early_entry = "early_entry"
    perfect_entry = "perfect_entry"
    late_entry = "late_entry"
    false_positive = "false_positive"
    pending = "pending"


class LearningEventCreate(BaseModel):
    source: str
    signal_type: str
    action: str
    outcome: SignalOutcome = SignalOutcome.pending
    features: dict[str, Any] = Field(default_factory=dict)


class LearningEvent(LearningEventCreate):
    id: str
    created_at: datetime


app = FastAPI(title="TradeOS AI Learning Engine", version="0.1.0")
events: list[LearningEvent] = []


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "learning-engine"}


@app.get("/events", response_model=list[LearningEvent])
async def list_events() -> list[LearningEvent]:
    return events


@app.post("/events", response_model=LearningEvent)
async def create_event(request: LearningEventCreate) -> LearningEvent:
    event = LearningEvent(id=str(uuid4()), created_at=datetime.now(UTC), **request.model_dump())
    events.append(event)
    return event

