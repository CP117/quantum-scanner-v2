
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def utcnowiso() -> str:
    return utcnow_iso()


def age_seconds_from_iso(value: str | None) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return max(0, int((utcnow() - dt).total_seconds()))
    except Exception:
        return 0


def agesecondsfromiso(value: str | None) -> int:
    return age_seconds_from_iso(value)


def freshness_label_from_age(age_seconds: int) -> str:
    if age_seconds < 15:
        return "fresh"
    if age_seconds < 60:
        return "recent"
    return "stale"


def freshnesslabelfromage(age_seconds: int) -> str:
    return freshness_label_from_age(age_seconds)
