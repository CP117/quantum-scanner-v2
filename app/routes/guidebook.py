"""Phase 26.50 — Guidebook download routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

router = APIRouter()
log = logging.getLogger(__name__)


@router.get('/api/guidebook/pdf')
def download_guidebook_pdf() -> Response:
    """Render the comprehensive metric guidebook as a PDF and return it
    as an attachment download."""
    try:
        from app.services.guidebook_pdf import render_guidebook_pdf
        pdf_bytes = render_guidebook_pdf()
    except Exception as exc:  # noqa: BLE001
        log.exception('guidebook pdf render failed')
        raise HTTPException(status_code=500, detail=f'pdf_render_failed: {exc}')
    return Response(
        content=pdf_bytes,
        media_type='application/pdf',
        headers={
            'Content-Disposition':
                'attachment; filename="market-refinement-dashboard-guidebook.pdf"',
            'Cache-Control': 'public, max-age=300',
            'X-MRD-Guidebook-Phase': '26.50',
        },
    )


@router.get('/api/guidebook/json')
def download_guidebook_json():
    """Same content as the PDF but as machine-readable JSON, for any
    third-party tooling that wants to consume the metric registry."""
    from app.services.guidebook_content import build_guidebook
    return build_guidebook()