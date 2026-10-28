from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from .. import __version__
from ..agent_client import AgentError, AgentUnavailable
from ..deps import AdminPrincipal, Agent

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/status")
def system_status(_admin: AdminPrincipal, agent: Agent):
    try:
        result = agent.call("system.status")
    except AgentUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except AgentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    result["panel_version"] = __version__
    return result
