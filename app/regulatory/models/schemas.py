from pydantic import BaseModel
from typing import Optional, List

class FilingEvent(BaseModel):
    accession_number: str
    filing_date: str
    form: str
    issuer_cik: Optional[str] = None
    issuer_ticker: Optional[str] = None
    issuer_name: Optional[str] = None
    reporting_owner_name: Optional[str] = None
    reporting_owner_cik: Optional[str] = None
    is_director: Optional[bool] = None
    is_officer: Optional[bool] = None
    is_ten_percent_owner: Optional[bool] = None
    is_other: Optional[bool] = None
    officer_title: Optional[str] = None
    transaction_code: Optional[str] = None
    transaction_type: Optional[str] = None
    security_type: Optional[str] = None
    shares: Optional[float] = None
    price_per_share: Optional[float] = None
    shares_owned_following: Optional[float] = None
    ownership_nature: Optional[str] = None
    percent_owned: Optional[float] = None
    source_url: Optional[str] = None
    raw_excerpt: Optional[str] = None

class AwardEvent(BaseModel):
    generated_internal_id: str
    award_id: Optional[str] = None
    recipient_name: Optional[str] = None
    recipient_uei: Optional[str] = None
    awarding_agency: Optional[str] = None
    awarding_subagency: Optional[str] = None
    action_date: Optional[str] = None
    amount: Optional[float] = None
    description: Optional[str] = None
    naics_code: Optional[str] = None
    naics_description: Optional[str] = None
    psc_code: Optional[str] = None
    psc_description: Optional[str] = None

class MonitorResponse(BaseModel):
    insider_events: List[FilingEvent]
    award_events: List[AwardEvent]
    insider_status: str = 'ok'
    awards_status: str = 'ok'
    insider_error: Optional[str] = None
    awards_error: Optional[str] = None
