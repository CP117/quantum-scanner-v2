"""
/api/api-keys/* routes — user-supplied API key management.

  GET    /api/api-keys             -> list which providers are configured (no raw values)
  POST   /api/api-keys/{provider}  -> set or update a key  (body: {"value": "..."})
  DELETE /api/api-keys/{provider}  -> remove a key
  GET    /api/api-keys/preview     -> masked-key preview for the Settings UI

The raw key value is NEVER returned over the wire to the client. The UI
only receives:
  - boolean "is configured" flags via `GET /api/api-keys`
  - a masked preview like `"fhn1…3xz"` via `GET /api/api-keys/preview`

This is a personal-use admin surface so we don't put auth in front of it;
the whole app already assumes the user controls the local machine.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import api_keys

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


class SetKeyBody(BaseModel):
    value: str


@router.get("")
def list_keys() -> dict:
    """Return {provider: bool} indicating which providers have a key configured."""
    return {
        "configured": api_keys.list_configured(),
        "supported": sorted(api_keys.SUPPORTED_PROVIDERS),
    }


@router.get("/preview")
def preview_keys() -> dict:
    """Return {provider: masked_key | null} for every configured provider.
    Used by the Settings panel to confirm a key was saved correctly
    without exposing the full value."""
    return {p: api_keys.key_preview(p) for p in sorted(api_keys.SUPPORTED_PROVIDERS)}


@router.post("/{provider}")
def set_key(provider: str, body: SetKeyBody) -> dict:
    if provider.lower() not in api_keys.SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unsupported provider '{provider}'")
    ok = api_keys.set_key(provider, body.value)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to store key")
    return {"ok": True, "provider": provider.lower(), "configured": bool(body.value.strip()), "preview": api_keys.key_preview(provider)}


@router.delete("/{provider}")
def delete_key(provider: str) -> dict:
    api_keys.delete(provider)
    return {"ok": True, "provider": provider.lower(), "configured": False}
