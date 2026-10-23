import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lsg_strength_sensor.config import LsgConfig, RulesConfig, SensorConfig, WgerConfig
from lsg_strength_sensor.crediting import CreditOutcome, OfflineSyncCreditor, safe_credit
from lsg_strength_sensor.http import NetworkError
from lsg_strength_sensor.models import CreditRecord, CreditStatus, WorkoutSet
from lsg_strength_sensor.pipeline import SensorPipeline
from lsg_strength_sensor.store import StateStore

TZ = ZoneInfo("America/Santiago")


def mk(log_id, day=13, minute=0, muscles=("chest",)):
    return WorkoutSet(
        log_id=log_id, performed_at=datetime(2026, 10, day, 18, minute, tzinfo=TZ),
        exercise_id=1, exercise_name="press", reps=10, weight_kg=60.0, primary_muscles=frozenset(muscles),
    )


class FakeWger:
    def __init__(self):
        self.sets: list[WorkoutSet] = []
        self._exercise_cache = {}

    def fetch_new_sets(self, after):
        return [s for s in self.sets if s.log_id > after]


class FakeCreditor:
    name = "fake"

    def __init__(self):
        self.fail = False
        self.received: list[CreditRecord] = []

    def credit(self, record):
        if self.fail:
            raise NetworkError("sin conexión")
        self.received.append(record)
        return CreditOutcome(CreditStatus.SYNCED, remote_ref="L1")


def make_cfg(db: Path) -> SensorConfig:
    return SensorConfig(
        timezone=TZ, state_db=db, poll_interval_minutes=30, start_date=None,
        rules=RulesConfig(), wger=WgerConfig(), lsg=LsgConfig(send_webhook=False),
    )


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "s.db")
        self.wger = FakeWger()
        self.creditor = FakeCreditor()
        self.p = SensorPipeline(make_cfg(Path(self.tmp.name) / "s.db"), self.store, self.wger,
                                self.creditor, core=None, player_id=50)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_rerun_does_not_double_credit(self):
        self.wger.sets = [mk(i, minute=i) for i in range(1, 7)]
        s1 = self.p.run_once()
        self.assertEqual(s1.points_enqueued, 20)
        s2 = self.p.run_once()
        self.assertEqual(s2.new_sets, 0)
        self.assertEqual(sum(r.amount for r in self.creditor.received), 20)

    def test_incremental_session_reaches_cap_once(self):
        self.wger.sets = [mk(i, minute=i) for i in range(1, 5)]       # 4 series → 10 pts
        self.p.run_once()
        self.wger.sets += [mk(i, minute=i) for i in range(5, 16)]     # total 15 → tope 30
        self.p.run_once()
        self.assertEqual(sum(r.amount for r in self.creditor.received), 30)
        self.assertEqual(self.store.credited_points_on(date(2026, 10, 13)), 30)

    def test_network_failure_keeps_pending_then_syncs(self):
        self.wger.sets = [mk(i, minute=i) for i in range(1, 4)]
        self.creditor.fail = True
        s1 = self.p.run_once()
        self.assertEqual(s1.credits_pending, 1)
        # Aunque el crédito está pendiente, ya cuenta para el tope del día.
        self.assertEqual(self.store.credited_points_on(date(2026, 10, 13)), 10)
        self.creditor.fail = False
        s2 = self.p.run_once()
        self.assertEqual(s2.credits_synced, 1)
        self.assertEqual(len(self.store.pending_credits()), 0)

    def test_late_logged_set_goes_to_its_own_day(self):
        self.wger.sets = [mk(1, day=13), mk(2, day=13, minute=1), mk(3, day=13, minute=2), mk(4, day=11, minute=0)]
        self.p.run_once()
        self.assertEqual(self.store.valid_sets_on(date(2026, 10, 11)), 1)
        self.assertEqual(self.store.credited_points_on(date(2026, 10, 13)), 10)



class OfflineSyncParsingTest(unittest.TestCase):
    def _record(self):
        return CreditRecord(client_ref="abc", local_day=date(2026, 10, 13), amount=10,
                            created_at=datetime(2026, 10, 13, 21, 0, tzinfo=TZ))

    def test_statuses(self):
        for remote, expected in [("SYNCED", CreditStatus.SYNCED), ("DUPLICATE", CreditStatus.DUPLICATE),
                                 ("REJECTED", CreditStatus.REJECTED)]:
            core = type("C", (), {"offline_sync": lambda self, ev, s=remote: {
                "results": [{"client_ref": "abc", "status": s, "id_points_ledger": 9}]}})()
            out = safe_credit(OfflineSyncCreditor(core, 2), self._record())
            self.assertEqual(out.status, expected)

    def test_network_error_is_pending(self):
        def boom(self, ev):
            raise NetworkError("x")
        core = type("C", (), {"offline_sync": boom})()
        self.assertEqual(safe_credit(OfflineSyncCreditor(core, 2), self._record()).status, CreditStatus.PENDING)


if __name__ == "__main__":
    unittest.main()
