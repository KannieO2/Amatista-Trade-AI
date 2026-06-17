from enum import StrEnum
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field


class Product(StrEnum):
    pump_reader = "pump_reader"
    grvtbot_pro = "grvtbot_pro"


class ApiKeyValidationRequest(BaseModel):
    exchange: str
    permissions: list[str] = Field(default_factory=list)


class ApiKeyValidationResponse(BaseModel):
    accepted: bool
    reason: str


class RiskLimits(BaseModel):
    max_daily_loss_usd: float = 250.0
    max_drawdown_pct: float = 5.0
    max_position_size_usd: float = 500.0
    max_open_trades: int = 3
    max_leverage: float = 2.0


class RiskEvaluationRequest(BaseModel):
    product: Product
    action: str
    position_size_usd: float = 0.0
    leverage: float = 1.0
    open_trades: int = 0
    daily_loss_usd: float = 0.0
    current_drawdown_pct: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RiskDecision(BaseModel):
    allowed: bool
    reason: str
    kill_switch_active: bool


class KillSwitchState(BaseModel):
    active: bool = False
    reason: str = "inactive"


app = FastAPI(title="TradeOS AI Risk Engine", version="0.1.0")
limits = RiskLimits()
kill_switch = KillSwitchState()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "risk-engine"}


@app.get("/limits", response_model=RiskLimits)
async def get_limits() -> RiskLimits:
    return limits


@app.post("/api-keys/validate", response_model=ApiKeyValidationResponse)
async def validate_api_key(request: ApiKeyValidationRequest) -> ApiKeyValidationResponse:
    normalized_permissions = {permission.lower() for permission in request.permissions}
    if "withdraw" in normalized_permissions or "withdrawal" in normalized_permissions:
        return ApiKeyValidationResponse(
            accepted=False,
            reason="API keys with withdrawal permissions are rejected automatically.",
        )

    return ApiKeyValidationResponse(accepted=True, reason="API key permissions accepted.")


@app.post("/risk/evaluate", response_model=RiskDecision)
async def evaluate_risk(request: RiskEvaluationRequest) -> RiskDecision:
    if kill_switch.active:
        return RiskDecision(allowed=False, reason=kill_switch.reason, kill_switch_active=True)

    if request.product == Product.pump_reader and request.action in {"buy", "sell", "execute_order"}:
        return RiskDecision(
            allowed=False,
            reason="Pump Reader cannot execute trades automatically; human approval is required.",
            kill_switch_active=False,
        )

    if request.position_size_usd > limits.max_position_size_usd:
        return RiskDecision(allowed=False, reason="Position size exceeds risk limit.", kill_switch_active=False)

    if request.leverage > limits.max_leverage:
        return RiskDecision(allowed=False, reason="Leverage exceeds risk limit.", kill_switch_active=False)

    if request.open_trades >= limits.max_open_trades:
        return RiskDecision(allowed=False, reason="Open trade limit reached.", kill_switch_active=False)

    if request.daily_loss_usd >= limits.max_daily_loss_usd:
        return RiskDecision(allowed=False, reason="Daily loss limit reached.", kill_switch_active=False)

    if request.current_drawdown_pct >= limits.max_drawdown_pct:
        return RiskDecision(allowed=False, reason="Drawdown limit reached.", kill_switch_active=False)

    return RiskDecision(allowed=True, reason="Risk checks passed.", kill_switch_active=False)


@app.get("/risk/kill-switch", response_model=KillSwitchState)
async def get_kill_switch() -> KillSwitchState:
    return kill_switch


@app.post("/risk/kill-switch", response_model=KillSwitchState)
async def set_kill_switch(state: KillSwitchState) -> KillSwitchState:
    kill_switch.active = state.active
    kill_switch.reason = state.reason if state.active else "inactive"
    return kill_switch

