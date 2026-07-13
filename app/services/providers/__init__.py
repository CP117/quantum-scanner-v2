"""
Pluggable multi-provider quote/options framework.

Provider tiers (run as a cascade, first usable wins; failures fall through):
  1. yfinance        - primary (full fast_info + intraday + download)
  2. yahoo-chart     - direct Yahoo chart API, no crumb required
  3. stooq           - free EOD CSV (no auth), great for `previous_close` backfill
  4. cryptocompare   - free crypto provider alongside coingecko
  5. (cache fallback handled outside the chain)

Each provider returns a dict matching the `QuoteRow` shape OR an empty dict.
A provider that returns `{}` simply yields to the next provider.
"""
