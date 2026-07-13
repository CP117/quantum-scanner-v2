
from pydantic import BaseModel, Field


class SearchResultRow(BaseModel):
    symbol: str
    name: str
    exchange: str


class SearchResultsEnvelope(BaseModel):
    query: str
    total: int
    results: list[SearchResultRow] = Field(default_factory=list)
