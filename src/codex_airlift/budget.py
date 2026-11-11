"""A pinned VM bounds monthly compute; a fixed lease requires price revalidation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re

HOURLY_CEILING = Decimal("0.125")
RESERVE_USD = Decimal("1")
MAX_MONTH_HOURS = 744
LEASE_DAYS = 90
MACHINE_TYPE = "e2-highmem-2"
ZONE = "us-central1-a"


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must contain a timezone")
    return result.astimezone(timezone.utc)


def make_lease(project, name, budget, codex_version, now=None):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("Invalid GCP project ID")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,38}[a-z0-9]", name):
        raise ValueError("Invalid service name (2–40 lowercase characters)")
    if not re.fullmatch(r"\d+\.\d+\.\d+", codex_version):
        raise ValueError("Pin a numeric Codex CLI version")
    amount = Decimal(str(budget))
    minimum = HOURLY_CEILING * MAX_MONTH_HOURS + RESERVE_USD
    if not amount.is_finite() or not minimum <= amount <= Decimal("100"):
        raise ValueError(f"Pinned host requires a monthly compute allowance between ${minimum} and $100")
    now = (now or utcnow()).replace(microsecond=0)
    return {
        "schema": 1, "project": project, "name": name, "zone": ZONE,
        "machine_type": MACHINE_TYPE, "compute_budget_usd": str(amount),
        "budget_period": "calendar-month", "maximum_monthly_compute_usd": str(HOURLY_CEILING * MAX_MONTH_HOURS),
        "hourly_ceiling_usd": str(HOURLY_CEILING), "budget_started_at": timestamp(now),
        "termination_time": timestamp(now + timedelta(days=LEASE_DAYS)),
        "codex_version": codex_version,
    }


def validate_existing(existing, requested, now=None):
    for field in ("project", "name", "compute_budget_usd", "codex_version"):
        if existing[field] != requested[field]:
            raise ValueError(f"Existing deployment differs in {field}; refusing an implicit reset/update")
    if parse_time(existing["termination_time"]) <= (now or utcnow()):
        raise ValueError("Compute lease expired; explicit renewed budget authorization is required")
    return existing
