from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from ..agent_client import AgentClient, AgentError, AgentUnavailable


def call_agent(
    agent: AgentClient, action: str, params: dict[str, Any] | None = None, timeout: float = 60
) -> Any:
    """Call the agent and turn its errors into HTTP errors the frontend can show."""
    try:
        return agent.call(action, params, timeout=timeout)
    except AgentUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except AgentError as exc:
        message = str(exc)
        if message == "internal error":
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "the server could not complete this action"
            ) from exc
        raise HTTPException(status.HTTP_400_BAD_REQUEST, message) from exc
