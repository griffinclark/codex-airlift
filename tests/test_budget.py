from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from codex_airlift.budget import make_lease, parse_time, validate_existing


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 30, 10, tzinfo=timezone.utc)
        self.lease = make_lease("example-project", "airlift", "100", "0.146.0", self.now)

    def test_full_month_cannot_exhaust_allowance_at_rate_ceiling(self):
        cost = Decimal(self.lease["hourly_ceiling_usd"]) * 31 * 24
        self.assertLess(cost, Decimal(self.lease["compute_budget_usd"]))
        self.assertEqual(self.lease["machine_type"], "e2-highmem-2")
        self.assertEqual(self.lease["budget_period"], "calendar-month")

    def test_reapply_does_not_extend_existing_deadline(self):
        later = self.now + timedelta(days=20)
        requested = make_lease("example-project", "airlift", "100", "0.146.0", later)
        saved = validate_existing(self.lease, requested, later)
        self.assertEqual(parse_time(saved["termination_time"]), self.now + timedelta(days=90))

    def test_expiry_and_implicit_budget_changes_rejected(self):
        with self.assertRaises(ValueError):
            validate_existing(self.lease, self.lease, self.now + timedelta(days=91))
        changed = dict(self.lease, compute_budget_usd="99")
        with self.assertRaises(ValueError):
            validate_existing(self.lease, changed, self.now)

    def test_unsafe_or_nonfinite_parameters_rejected(self):
        for value in ("0", "93", "101", "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                make_lease("example-project", "airlift", value, "0.146.0", self.now)
        with self.assertRaises(ValueError):
            make_lease("example-project", "bad;touch x", "100", "0.146.0", self.now)
        with self.assertRaises(ValueError):
            make_lease("example-project", "airlift", "100", "latest;command", self.now)

    def test_explicit_version_update_preserves_budget_anchor(self):
        changed = dict(self.lease, codex_version="0.167.1")
        with self.assertRaises(ValueError):
            validate_existing(self.lease, changed, self.now)
        updated = validate_existing(self.lease, changed, self.now, update_codex=True)
        self.assertEqual(updated, changed)
        self.assertEqual(updated["budget_started_at"], self.lease["budget_started_at"])
        self.assertEqual(updated["termination_time"], self.lease["termination_time"])
