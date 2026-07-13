"""Phase 26.50 — PDF guidebook renderer.

Uses reportlab (pure Python, no system deps) to render the structured
guidebook from `guidebook_content.build_guidebook()` into a polished
multi-page PDF.

Design goals:
* Single-column letter portrait, comfortable reading width
* Section headings + colored tier badges so each tier is visually
  distinct
* Each metric: bold label, then three labelled paragraphs (What it is,
  How to read it, Impact)
* Final pages: blending rules + tips & tricks bullet list
* No external image assets — fully self-contained
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.services.guidebook_content import build_guidebook

# ---------------------------------------------------------------------------
# Visual tokens — kept here (not in a CSS file) so the PDF stays
# self-contained.  Colours echo the dashboard's dark theme but on a
# white background for print clarity.
# ---------------------------------------------------------------------------
_TONE_COLOR = {
    'per_horizon':   colors.HexColor('#1d4ed8'),  # blue
    'advanced_math': colors.HexColor('#7c3aed'),  # purple
    'lab':           colors.HexColor('#0891b2'),  # cyan
    'strategy':      colors.HexColor('#16a34a'),  # green
}
_MUTED = colors.HexColor('#475569')
_TEXT = colors.HexColor('#0f172a')
_RULE = colors.HexColor('#cbd5e1')


def _build_styles() -> dict:
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        'Body', parent=base['BodyText'], fontName='Helvetica',
        fontSize=10, leading=14, textColor=_TEXT, spaceAfter=6,
    )
    h1 = ParagraphStyle(
        'H1', parent=base['Heading1'], fontName='Helvetica-Bold',
        fontSize=20, leading=24, textColor=_TEXT, spaceAfter=10,
    )
    h2 = ParagraphStyle(
        'H2', parent=base['Heading2'], fontName='Helvetica-Bold',
        fontSize=14, leading=18, textColor=_TEXT, spaceBefore=10, spaceAfter=6,
    )
    h3 = ParagraphStyle(
        'H3', parent=base['Heading3'], fontName='Helvetica-Bold',
        fontSize=11, leading=14, textColor=_TEXT, spaceBefore=8, spaceAfter=2,
    )
    label = ParagraphStyle(
        'Label', parent=body, fontName='Helvetica-Bold',
        fontSize=10.5, leading=14, textColor=_TEXT, spaceBefore=8, spaceAfter=2,
    )
    sub = ParagraphStyle(
        'Sub', parent=body, fontSize=9, leading=13, textColor=_MUTED,
        spaceAfter=4,
    )
    tip = ParagraphStyle(
        'Tip', parent=body, fontSize=10, leading=14, leftIndent=14,
        bulletIndent=4, spaceAfter=4,
    )
    bullet = ParagraphStyle(
        'Bullet', parent=body, fontSize=10, leading=14, leftIndent=14,
        bulletIndent=4, spaceAfter=4,
    )
    return {
        'body': body, 'h1': h1, 'h2': h2, 'h3': h3,
        'label': label, 'sub': sub, 'tip': tip, 'bullet': bullet,
    }


def _section_band(title: str, tone_color) -> Table:
    """A coloured band that doubles as a chapter break marker."""
    t = Table(
        [[Paragraph(f'<font color="white"><b>{title}</b></font>',
                    ParagraphStyle('band', fontSize=12, leading=16, textColor=colors.white))]],
        colWidths=[6.5 * inch],
        rowHeights=[0.34 * inch],
    )
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), tone_color),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOX', (0, 0), (-1, -1), 0, tone_color),
    ]))
    return t


def _metric_block(metric: dict, styles: dict) -> KeepTogether:
    """A single metric's mini-section, kept on one page where possible."""
    label_html = f'<b>{_esc(metric["label"])}</b>  ' \
                 f'<font size="8" color="#64748b">({_esc(metric["key"])})</font>'
    return KeepTogether([
        Paragraph(label_html, styles['label']),
        Paragraph(f'<b>What it is.</b> {_esc(metric["summary"])}', styles['body']),
        Paragraph(f'<b>How to read it.</b> {_esc(metric["interpretation"])}', styles['body']),
        Paragraph(f'<b>Impact on ranking.</b> {_esc(metric["impact"])}', styles['body']),
        Spacer(1, 4),
    ])


def _esc(s: str) -> str:
    """ReportLab paragraph mini-HTML escape.  Allows our own <b>/<font> tags."""
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _on_page(canvas, doc):
    """Header / footer painter."""
    canvas.saveState()
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(_MUTED)
    canvas.drawString(0.6 * inch, 0.4 * inch,
                      f'Market Refinement Dashboard — Metric Guidebook')
    canvas.drawRightString(letter[0] - 0.6 * inch, 0.4 * inch,
                           f'Page {doc.page}')
    canvas.setStrokeColor(_RULE)
    canvas.line(0.6 * inch, 0.55 * inch, letter[0] - 0.6 * inch, 0.55 * inch)
    canvas.restoreState()


def render_guidebook_pdf() -> bytes:
    """Render the guidebook PDF and return the raw bytes."""
    payload = build_guidebook()
    styles = _build_styles()
    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.6 * inch,  bottomMargin=0.7 * inch,
        title='Market Refinement Dashboard — Metric Guidebook',
        author='Market Refinement Dashboard',
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin,
                  doc.width, doc.height, showBoundary=0)
    doc.addPageTemplates(PageTemplate(id='main', frames=frame, onPage=_on_page))

    story = []
    # --- Cover ---
    story.append(Spacer(1, 1.0 * inch))
    story.append(Paragraph(f'<b>{_esc(payload["title"])}</b>', styles['h1']))
    story.append(Paragraph(_esc(payload['subtitle']), styles['h2']))
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(_esc(payload['intro_text']), styles['body']))
    story.append(Spacer(1, 0.4 * inch))
    generated = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    story.append(Paragraph(f'Generated: {generated}', styles['sub']))
    story.append(PageBreak())

    # --- Table of contents ---
    story.append(Paragraph('Table of Contents', styles['h1']))
    story.append(Spacer(1, 0.1 * inch))
    toc_lines = [
        '1. How to read the Future Forecast cell',
        '2. Blending rules & ranking impact',
    ]
    for i, sec in enumerate(payload['sections'], start=3):
        toc_lines.append(f'{i}. {sec["title"]}')
    toc_lines.append(f'{len(payload["sections"]) + 3}. Tips & tricks')
    for line in toc_lines:
        story.append(Paragraph(line, styles['body']))
    story.append(PageBreak())

    # --- How to read the cell ---
    story.append(_section_band('1.  How to read the Future Forecast cell',
                               colors.HexColor('#0f172a')))
    story.append(Spacer(1, 0.15 * inch))
    for heading, paragraphs in payload['how_to_read']:
        story.append(Paragraph(_esc(heading), styles['h3']))
        for p in paragraphs:
            story.append(Paragraph(_esc(p), styles['body']))
    story.append(PageBreak())

    # --- Blending rules ---
    story.append(_section_band('2.  Blending rules & ranking impact',
                               colors.HexColor('#0f172a')))
    story.append(Spacer(1, 0.15 * inch))
    for heading, paragraphs in payload['blending_rules']:
        story.append(Paragraph(_esc(heading), styles['h3']))
        for p in paragraphs:
            story.append(Paragraph(_esc(p), styles['body']))
    story.append(PageBreak())

    # --- Metric sections ---
    for idx, sec in enumerate(payload['sections'], start=3):
        sec_id = next((s['id'] for s in __import__('app.services.guidebook_content',
                                                   fromlist=['SECTIONS']).SECTIONS
                       if s['title'] == sec['title']), 'per_horizon')
        tone = _TONE_COLOR.get(sec_id, colors.HexColor('#1d4ed8'))
        story.append(_section_band(f'{idx}.  {sec["title"]}', tone))
        story.append(Spacer(1, 0.12 * inch))
        story.append(Paragraph(_esc(sec['intro']), styles['body']))
        story.append(Spacer(1, 0.05 * inch))
        for metric in sec['metrics']:
            story.append(_metric_block(metric, styles))
        story.append(PageBreak())

    # --- Tips & tricks ---
    story.append(_section_band(
        f'{len(payload["sections"]) + 3}.  Tips & tricks',
        colors.HexColor('#0f172a'),
    ))
    story.append(Spacer(1, 0.15 * inch))
    for tip in payload['tips']:
        story.append(Paragraph(f'•  {_esc(tip)}', styles['tip']))

    doc.build(story)
    return buf.getvalue()
