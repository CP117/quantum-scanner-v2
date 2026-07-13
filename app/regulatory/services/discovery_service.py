import hashlib
import re
import xml.etree.ElementTree as ET
from app.regulatory.services.storage_service import upsert_tracked_company, create_alert, save_discovery_event
from app.regulatory.services.http_client import get_client

INTEREST_FORMS = {'3', '4', '5', 'SC 13D', 'SC 13D/A', 'SC 13G', 'SC 13G/A'}
FEED_URL = 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&company=&dateb=&owner=only&start=0&count=100&output=atom'


def _clean(text: str):
    return re.sub(r'\s+', ' ', (text or '')).strip()


def _extract_form(entry) -> str:
    category = entry.find('{http://www.w3.org/2005/Atom}category')
    if category is not None:
        term = category.attrib.get('term')
        if term:
            return term.strip()
    summary = entry.find('{http://www.w3.org/2005/Atom}summary')
    if summary is not None and summary.text:
        m = re.search(r'Filed:\s*([A-Z0-9\-/ ]+)', summary.text)
        if m:
            return m.group(1).strip()
    return ''


def _extract_cik(link: str) -> str:
    m = re.search(r'/data/(\d+)/', link or '')
    return m.group(1) if m else ''

async def discover_new_insider_companies(limit: int = 100):
    client = get_client('sec')
    r = await client.get(FEED_URL)
    r.raise_for_status()
    xml_text = r.text
    root = ET.fromstring(xml_text)
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    entries = root.findall('a:entry', ns)
    discovered = 0
    for entry in entries[:limit]:
        title = _clean(entry.findtext('a:title', default='', namespaces=ns))
        updated = _clean(entry.findtext('a:updated', default='', namespaces=ns))
        link_el = entry.find('a:link', ns)
        filing_url = link_el.attrib.get('href') if link_el is not None else ''
        form_type = _extract_form(entry)
        if form_type not in INTEREST_FORMS:
            continue
        cik = _extract_cik(filing_url)
        if not cik:
            continue
        issuer_name = title.split('(')[0].strip() if title else cik
        event_key = hashlib.sha256(f'{cik}|{form_type}|{updated}|{filing_url}'.encode()).hexdigest()
        is_new = await save_discovery_event(event_key, cik=cik, issuer_name=issuer_name, form_type=form_type, filing_date=updated, filing_url=filing_url)
        if not is_new:
            continue
        await upsert_tracked_company(cik=cik, issuer_name=issuer_name, issuer_ticker=None, filing_date=updated, source='sec_latest_filing_event')
        await create_alert('auto_discovery', f'discovery:{event_key}', 'New insider filing company discovered', f'{issuer_name} | CIK {cik} | form {form_type} | {updated}')
        discovered += 1
    return discovered
