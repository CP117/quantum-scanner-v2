import re
import httpx
from typing import List, Dict, Any, Optional
from xml.etree import ElementTree as ET
from app.regulatory.models.schemas import FilingEvent
from app.regulatory.services.http_client import get_client

SEC_HEADERS = {
    "User-Agent": "MarketRefinementDashboard/1.0 admin@localhost",
    "Accept-Encoding": "gzip, deflate",
}

INTEREST_FORMS = {"3", "4", "5", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}
TXN_MAP = {
    "P": "open_market_buy",
    "S": "open_market_sell",
    "A": "grant_or_award",
    "D": "disposition_to_issuer",
    "F": "tax_withholding_or_payment",
    "M": "derivative_exercise",
    "G": "gift",
    "J": "other",
    "K": "equity_swap",
    "V": "early_report",
}

def cik10(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits.zfill(10)

def accession_no_dashes(accession: str) -> str:
    return accession.replace("-", "")

async def get_company_submissions(cik: str) -> Dict[str, Any]:
    url = f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json"
    client = get_client('sec')
    r = await client.get(url)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()

def build_filing_index(submissions: Dict[str, Any]) -> List[Dict[str, Any]]:
    recent = submissions.get("filings", {}).get("recent", {}) if submissions else {}
    forms = recent.get("form", [])
    out = []
    for i, form in enumerate(forms):
        if form not in INTEREST_FORMS:
            continue
        out.append({
            "form": form,
            "filingDate": recent.get("filingDate", [None])[i],
            "accessionNumber": recent.get("accessionNumber", [None])[i],
            "primaryDocument": recent.get("primaryDocument", [None])[i],
            "primaryDocDescription": recent.get("primaryDocDescription", [None])[i],
        })
    return out

async def fetch_filing_text(cik: str, accession: str, primary_document: str) -> str:
    cik_num = str(int(cik10(cik)))
    acc = accession_no_dashes(accession)
    # SEC's submissions API now returns primary_document with a stylesheet
    # prefix like `xslF345X06/form4.xml` which yields HTML, not the raw XML
    # we need to parse. Strip any xsl*/ prefix to get the underlying XML.
    cleaned = re.sub(r'^xsl[A-Za-z0-9]+/', '', primary_document or '')
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc}/{cleaned}"
    client = get_client('sec')
    r = await client.get(url)
    if r.status_code == 404:
        # Fall back to the original (styled) URL in case the stylesheet
        # prefix was actually meaningful (some legacy filings).
        r = await client.get(
            f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc}/{primary_document}"
        )
        if r.status_code == 404:
            return ""
    r.raise_for_status()
    return r.text

def _text_or_none(node: Optional[ET.Element], path: str) -> Optional[str]:
    if node is None:
        return None
    el = node.find(path)
    return el.text.strip() if el is not None and el.text else None

def parse_form345_xml(xml_text: str, accession: str, filing_date: str, form: str) -> List[FilingEvent]:
    events: List[FilingEvent] = []
    root = ET.fromstring(xml_text)
    issuer = root.find("issuer")
    owner = root.find("reportingOwner")
    owner_rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    owner_id = owner.find("reportingOwnerId") if owner is not None else None
    issuer_cik = _text_or_none(issuer, "issuerCik")
    issuer_name = _text_or_none(issuer, "issuerName")
    issuer_ticker = _text_or_none(issuer, "issuerTradingSymbol")
    reporting_owner_name = _text_or_none(owner_id, "rptOwnerName")
    reporting_owner_cik = _text_or_none(owner_id, "rptOwnerCik")
    base = dict(
        accession_number=accession,
        filing_date=filing_date,
        form=form,
        issuer_cik=issuer_cik,
        issuer_ticker=issuer_ticker,
        issuer_name=issuer_name,
        reporting_owner_name=reporting_owner_name,
        reporting_owner_cik=reporting_owner_cik,
        is_director=_text_or_none(owner_rel, "isDirector") == "1",
        is_officer=_text_or_none(owner_rel, "isOfficer") == "1",
        is_ten_percent_owner=_text_or_none(owner_rel, "isTenPercentOwner") == "1",
        is_other=_text_or_none(owner_rel, "isOther") == "1",
        officer_title=_text_or_none(owner_rel, "officerTitle"),
        source_url=None,
        raw_excerpt=None,
    )
    for table_name, security_type in [("nonDerivativeTable", "non_derivative"), ("derivativeTable", "derivative")]:
        table = root.find(table_name)
        if table is None:
            continue
        for txn in list(table):
            # Phase 26.13: only process actual TRANSACTION rows. Form 4 XML
            # also contains <nonDerivativeHolding> / <derivativeHolding>
            # children which describe post-transaction holdings (no shares
            # changed hands) - including those would create phantom events
            # with `shares=None, price=None, value=0`, which then dominated
            # the "freshest event" pick in signal_service and made every
            # multi-line Form 4 render as "$0.00M notional".
            tag = txn.tag.lower() if isinstance(txn.tag, str) else ''
            if 'transaction' not in tag:
                continue
            code = _text_or_none(txn, ".//transactionCoding/transactionCode")
            shares = _text_or_none(txn, ".//transactionAmounts/transactionShares/value")
            pps = _text_or_none(txn, ".//transactionAmounts/transactionPricePerShare/value")
            owned_following = _text_or_none(txn, ".//postTransactionAmounts/sharesOwnedFollowingTransaction/value")
            ownership_nature = _text_or_none(txn, ".//ownershipNature/directOrIndirectOwnership/value")
            # Phase 26.13: for derivative grants/awards the price-per-share
            # column is often "0" because it represents the cost basis, not
            # the underlying market value. Fall back to the exercise/conversion
            # price so the notional isn't artificially zeroed.
            if (not pps or float(pps or 0) == 0) and security_type == 'derivative':
                pps = _text_or_none(txn, ".//conversionOrExercisePrice/value") or pps
            events.append(FilingEvent(
                **base,
                transaction_code=code,
                transaction_type=TXN_MAP.get(code, "other") if code else None,
                security_type=security_type,
                shares=float(shares) if shares else None,
                price_per_share=float(pps) if pps else None,
                shares_owned_following=float(owned_following) if owned_following else None,
                ownership_nature=ownership_nature,
            ))
    if not events:
        events.append(FilingEvent(**base))
    return events

def parse_13dg_text(text: str, accession: str, filing_date: str, form: str, cik: str) -> List[FilingEvent]:
    clean = re.sub(r'\s+', ' ', text)
    names = []
    m_names = re.search(r'NAMES OF REPORTING PERSONS(.*?)(CHECK THE APPROPRIATE BOX|SOURCE OF FUNDS|CITIZENSHIP OR PLACE OF ORGANIZATION)', clean, re.I)
    if m_names:
        blob = m_names.group(1)
        parts = re.split(r'\b(?:IRS IDENTIFICATION NOS\.|SOLE VOTING POWER|SHARED VOTING POWER)\b', blob, maxsplit=1)
        candidate = parts[0].strip(' ;,')
        if candidate:
            names.append(candidate[:300])
    m_issuer = re.search(r'\(Name of Issuer\)\s*(.*?)\s*\(Title of Class of Securities\)', text, re.S | re.I)
    issuer_name = re.sub(r'\s+', ' ', m_issuer.group(1)).strip()[:300] if m_issuer else None
    pct_matches = re.findall(r'PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW \(11\)\s*([0-9]+(?:\.[0-9]+)?)%', clean, re.I)
    pct = float(pct_matches[0]) if pct_matches else None
    shared_power = None
    sole_power = None
    m_sole = re.search(r'SOLE VOTING POWER\s*([0-9,]+)', clean, re.I)
    m_shared = re.search(r'SHARED VOTING POWER\s*([0-9,]+)', clean, re.I)
    if m_sole:
        sole_power = float(m_sole.group(1).replace(',', ''))
    if m_shared:
        shared_power = float(m_shared.group(1).replace(',', ''))
    owner_name = names[0] if names else None
    excerpt = clean[:1200]
    return [FilingEvent(
        accession_number=accession,
        filing_date=filing_date,
        form=form,
        issuer_cik=cik10(cik),
        issuer_name=issuer_name,
        reporting_owner_name=owner_name,
        percent_owned=pct,
        shares=sole_power or shared_power,
        ownership_nature='shared' if shared_power and not sole_power else 'sole' if sole_power else None,
        transaction_type='beneficial_ownership_report',
        raw_excerpt=excerpt,
    )]

async def collect_interest_events(cik: str, limit: int = 20) -> List[FilingEvent]:
    submissions = await get_company_submissions(cik)
    filing_index = build_filing_index(submissions)[:limit]
    events: List[FilingEvent] = []
    if not filing_index:
        return events
    for filing in filing_index:
        accession = filing.get('accessionNumber')
        form = filing.get('form')
        primary_document = filing.get('primaryDocument')
        filing_date = filing.get('filingDate')
        if not accession or not primary_document:
            continue
        try:
            text = await fetch_filing_text(cik, accession, primary_document)
            if not text:
                continue
            if form in {'3', '4', '5'} and ((primary_document or '').lower().endswith('.xml') or '<ownershipDocument>' in text):
                parsed = parse_form345_xml(text, accession, filing_date, form)
            else:
                parsed = parse_13dg_text(text, accession, filing_date, form, cik)
            cik_num = str(int(cik10(cik)))
            acc = accession_no_dashes(accession)
            for item in parsed:
                item.source_url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc}/{primary_document}"
                events.append(item)
        except Exception:
            continue
    return events
