"""
Watchlist & Pin/Unpin API Routes — Phase 28
=============================================

Endpoints
---------
    POST   /api/watchlist/create                       — create watchlist
    GET    /api/watchlist/list                         — list all watchlists
    DELETE /api/watchlist/{watchlist_id}               — delete watchlist
    POST   /api/watchlist/{watchlist_id}/add-symbol    — add symbol
    POST   /api/watchlist/{watchlist_id}/remove-symbol — remove symbol
    GET    /api/watchlist/{watchlist_id}/symbols       — list symbols
    POST   /api/symbol/{symbol}/pin                    — pin to Tier 2 minimum
    POST   /api/symbol/{symbol}/unpin                  — unpin
    GET    /api/symbol/{symbol}/pin-status             — is pinned?
    GET    /api/watchlist/pinned                       — all pinned symbols
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix='/api/watchlist', tags=['watchlist'])
symbol_router = APIRouter(prefix='/api/symbol', tags=['watchlist'])


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class CreateWatchlistRequest(BaseModel):
    name: str
    user_id: str = 'default'


class AddRemoveSymbolRequest(BaseModel):
    symbol: str
    user_id: str = 'default'


# ---------------------------------------------------------------------------
# Watchlist CRUD
# ---------------------------------------------------------------------------

@router.post('/create')
def create_watchlist(req: CreateWatchlistRequest):
    """Create a new watchlist and return its record."""
    try:
        from app.services.watchlist_service import create_watchlist as _create
        return _create(req.name, req.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get('/list')
def list_watchlists(user_id: str = Query('default')):
    """List all watchlists for the given user."""
    try:
        from app.services.watchlist_service import list_watchlists as _list
        return {'watchlists': _list(user_id)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get('/pinned')
def get_pinned_symbols(user_id: str = Query('default')):
    """Return all currently pinned symbols for the given user."""
    try:
        from app.services.watchlist_service import get_pinned_symbols as _pinned
        return {'pinned': _pinned(user_id)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete('/{watchlist_id}')
def delete_watchlist(watchlist_id: int, user_id: str = Query('default')):
    """Delete a watchlist by ID."""
    try:
        from app.services.watchlist_service import delete_watchlist as _delete
        deleted = _delete(watchlist_id, user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f'Watchlist {watchlist_id} not found')
        return {'deleted': True, 'watchlist_id': watchlist_id}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post('/{watchlist_id}/add-symbol')
def add_symbol_to_watchlist(watchlist_id: int, req: AddRemoveSymbolRequest):
    """Add a symbol to a watchlist."""
    try:
        from app.services.watchlist_service import add_symbol as _add
        return _add(watchlist_id, req.symbol, req.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post('/{watchlist_id}/remove-symbol')
def remove_symbol_from_watchlist(watchlist_id: int, req: AddRemoveSymbolRequest):
    """Remove a symbol from a watchlist."""
    try:
        from app.services.watchlist_service import remove_symbol as _remove
        return _remove(watchlist_id, req.symbol, req.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get('/{watchlist_id}/symbols')
def get_watchlist_symbols(watchlist_id: int, user_id: str = Query('default')):
    """Return the symbols in a watchlist."""
    try:
        from app.services.watchlist_service import get_symbols as _syms
        return {'watchlist_id': watchlist_id, 'symbols': _syms(watchlist_id, user_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Pin / unpin
# ---------------------------------------------------------------------------

@symbol_router.post('/{symbol}/pin')
def pin_symbol(symbol: str, user_id: str = Query('default')):
    """Pin *symbol* to Tier 2 minimum."""
    try:
        from app.services.watchlist_service import pin_symbol as _pin
        return _pin(symbol, user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@symbol_router.post('/{symbol}/unpin')
def unpin_symbol(symbol: str, user_id: str = Query('default')):
    """Unpin *symbol* (allow future demotion to Tier 3)."""
    try:
        from app.services.watchlist_service import unpin_symbol as _unpin
        return _unpin(symbol, user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@symbol_router.get('/{symbol}/pin-status')
def pin_status(symbol: str, user_id: str = Query('default')):
    """Check whether *symbol* is currently pinned."""
    try:
        from app.services.watchlist_service import is_pinned as _is_pinned
        return {'symbol': symbol.upper(), 'pinned': _is_pinned(symbol, user_id)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
