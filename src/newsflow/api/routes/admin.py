"""Admin endpoints: operational actions gated by the API key.

POST /api/admin/reload — hot-reload webhooks.yaml + sources.yaml (the
in-process equivalent of SIGHUP). A file that fails to parse keeps its
previous synced state; the error comes back in the response instead of
disturbing the running bot.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from newsflow.api.deps import require_api_key

router = APIRouter()


class ReloadResponse(BaseModel):
    ok: bool
    detail: str


@router.post("/reload", response_model=ReloadResponse)
async def reload_configs(
    _: None = Depends(require_api_key),
) -> ReloadResponse:
    from newsflow.services.config_reload import reload_declarative_configs

    result = await reload_declarative_configs()
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.detail,
        )
    return ReloadResponse(ok=True, detail=result.detail)
