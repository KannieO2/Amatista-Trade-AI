from datetime import UTC, datetime
from enum import StrEnum

from fastapi import FastAPI
from pydantic import BaseModel


class Channel(StrEnum):
    dashboard = "dashboard"
    telegram = "telegram"
    email = "email"


class NotificationRequest(BaseModel):
    channel: Channel
    title: str
    body: str


class NotificationReceipt(BaseModel):
    accepted: bool
    channel: Channel
    created_at: datetime


app = FastAPI(title="TradeOS AI Notification Engine", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "notification-engine"}


@app.post("/notifications", response_model=NotificationReceipt)
async def create_notification(request: NotificationRequest) -> NotificationReceipt:
    return NotificationReceipt(accepted=True, channel=request.channel, created_at=datetime.now(UTC))

