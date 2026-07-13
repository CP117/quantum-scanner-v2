import hashlib
from typing import List, Dict, Any
from app.regulatory.models.schemas import AwardEvent
from app.regulatory.services.http_client import get_client

USA_BASE = "https://api.usaspending.gov/api/v2"

async def recipient_autocomplete(recipient_name: str):
    payload = {"search_text": recipient_name}
    client = get_client('usaspending')
    r = await client.post(f"{USA_BASE}/autocomplete/recipient/", json=payload)
    if r.status_code >= 400:
        return []
    data = r.json()
    return data.get("results", []) if isinstance(data, dict) else []

async def search_contract_awards(recipient_name: str, limit: int = 10) -> List[AwardEvent]:
    recipients = await recipient_autocomplete(recipient_name)
    matched_names = [x.get('recipient_name') for x in recipients if x.get('recipient_name')]
    search_names = matched_names[:3] if matched_names else [recipient_name]
    payload: Dict[str, Any] = {
        "filters": {
            "recipient_search_text": search_names,
            "award_type_codes": ["02", "03", "04", "05", "06", "07", "08"]
        },
        "limit": limit,
        "page": 1,
        "sort": "recipient_name",
        "order": "asc",
        "subawards": False
    }
    client = get_client('usaspending')
    r = await client.post(f"{USA_BASE}/search/spending_by_category/recipient", json=payload)
    if r.status_code >= 400:
        return []
    data = r.json()
    results = []
    rows = data.get("results", []) if isinstance(data, dict) else []
    for row in rows[:limit]:
        recipient_label = row.get("name") or row.get("recipient_name") or recipient_name
        amount = row.get("amount") or row.get("award_amount") or row.get("total_transaction_obligated_amount")
        rid = hashlib.md5(str(row).encode()).hexdigest()[:16]
        results.append(AwardEvent(
            generated_internal_id=rid,
            award_id=row.get("id") or row.get("award_id"),
            recipient_name=recipient_label,
            recipient_uei=row.get("uei"),
            awarding_agency=row.get("awarding_agency_name") or row.get("awarding_agency") or "",
            awarding_subagency=row.get("awarding_subagency_name") or row.get("awarding_subagency") or "",
            action_date=row.get("latest_transaction_date") or row.get("action_date"),
            amount=float(amount) if amount not in (None, "") else None,
            description=row.get("description") or "Recipient aggregate result",
            naics_code=row.get("naics_code"),
            naics_description=row.get("naics_description"),
            psc_code=row.get("psc_code"),
            psc_description=row.get("psc_description"),
        ))
    return results
