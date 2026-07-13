
import math
from app.config import settings
from app.services.universe_service import get_universe


def get_total_batches(limit: int | None = None, market: str = 'stocks') -> int:
    size = limit or settings.batch_size
    universe = get_universe(market)
    return max(1, math.ceil(len(universe) / size))


def get_batch_slice(batch: int, limit: int | None = None, market: str = 'stocks') -> list[dict]:
    size = limit or settings.batch_size
    total_batches = get_total_batches(size, market)
    b = batch % total_batches
    universe = get_universe(market)
    start = b * size
    end = start + size
    return universe[start:end]
