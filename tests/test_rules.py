import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from lsg_strength_sensor.config import RulesConfig
from lsg_strength_sensor.models import RejectReason, WorkoutSet
from lsg_strength_sensor.rules import DayContext, evaluate_day, make_client_ref, points_for_sets

TZ = ZoneInfo("America/Santiago")
DAY = date(2026, 10, 13)


def mk(log_id, hour=18, minute=0, exercise=1, reps=10, weight=60.0, muscles=("chest",), day=DAY):
    return WorkoutSet(
        log_id=log_id,
        performed_at=datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ),
        exercise_id=exercise, exercise_name=f"ex{exercise}", reps=reps, weight_kg=weight,
        primary_muscles=frozenset(muscles),
    )


def ctx(**kw):
    base = dict(muscle_last_trained={}, exercise_bests={}, training_days_this_week=set(),
                valid_sets_already=0, credited_already=0)
    base.update(kw)
    return DayContext(**base)


class PointsTest(unittest.TestCase):
    def test_formula(self):
        r = RulesConfig()
        self.assertEqual([points_for_sets(n, r) for n in (0, 2, 3, 5, 6, 9, 20)], [0, 0, 10, 10, 20, 30, 60])

    def test_daily_cap(self):
        ev = evaluate_day(DAY, [mk(i, minute=i) for i in range(1, 16)], ctx(), RulesConfig(), TZ)
        self.assertEqual(ev.valid_sets, 15)
        self.assertEqual(ev.raw_points, 50)
        self.assertEqual(ev.credited_points, 30)

    def test_blocks_accumulate_across_passes(self):
        r = RulesConfig()
        # Primera pasada: 2 series → 0 pts. Segunda: 1 serie más → completa bloque.
        ev1 = evaluate_day(DAY, [mk(1), mk(2, minute=5)], ctx(), r, TZ)
        self.assertEqual(ev1.credited_points, 0)
        ev2 = evaluate_day(DAY, [mk(3, minute=10)], ctx(valid_sets_already=2), r, TZ)
        self.assertEqual(ev2.credited_points, 10)

    def test_cap_respects_previous_credits(self):
        ev = evaluate_day(DAY, [mk(i, minute=i) for i in range(10, 16)], ctx(valid_sets_already=9, credited_already=30),
                          RulesConfig(), TZ)
        self.assertEqual(ev.credited_points, 0)


class ValidationTest(unittest.TestCase):
    def test_implausible(self):
        ev = evaluate_day(DAY, [mk(1, reps=0), mk(2, reps=200), mk(3, weight=900)], ctx(), RulesConfig(), TZ)
        self.assertEqual(ev.valid_sets, 0)
        self.assertIn(RejectReason.IMPLAUSIBLE_REPS, ev.verdicts[0].reasons)
        self.assertIn(RejectReason.IMPLAUSIBLE_WEIGHT, ev.verdicts[2].reasons)

    def test_cooldown_blocks_recent_previous_day(self):
        yesterday = datetime(2026, 10, 12, 19, 0, tzinfo=TZ)
        ev = evaluate_day(DAY, [mk(1)], ctx(muscle_last_trained={"chest": yesterday}), RulesConfig(), TZ)
        self.assertEqual(ev.verdicts[0].reasons, [RejectReason.MUSCLE_COOLDOWN])

    def test_cooldown_allows_other_muscles(self):
        yesterday = datetime(2026, 10, 12, 19, 0, tzinfo=TZ)
        ev = evaluate_day(DAY, [mk(1, muscles=("quads",))], ctx(muscle_last_trained={"chest": yesterday}),
                          RulesConfig(), TZ)
        self.assertTrue(ev.verdicts[0].valid)

    def test_cooldown_expired(self):
        three_days_ago = datetime(2026, 10, 10, 18, 0, tzinfo=TZ)
        ev = evaluate_day(DAY, [mk(1)], ctx(muscle_last_trained={"chest": three_days_ago}), RulesConfig(), TZ)
        self.assertTrue(ev.verdicts[0].valid)

    def test_same_day_sets_not_blocked_by_cooldown(self):
        earlier_today = datetime(2026, 10, 13, 17, 0, tzinfo=TZ)
        ev = evaluate_day(DAY, [mk(5)], ctx(muscle_last_trained={"chest": earlier_today}, valid_sets_already=4),
                          RulesConfig(), TZ)
        self.assertTrue(ev.verdicts[0].valid)

    def test_unrealistic_progression(self):
        ev = evaluate_day(DAY, [mk(1, weight=130)], ctx(exercise_bests={1: 100.0}), RulesConfig(), TZ)
        self.assertIn(RejectReason.UNREALISTIC_PROGRESSION, ev.verdicts[0].reasons)
        ok = evaluate_day(DAY, [mk(1, weight=115)], ctx(exercise_bests={1: 100.0}), RulesConfig(), TZ)
        self.assertTrue(ok.verdicts[0].valid)

    def test_weekly_frequency(self):
        week = {DAY - timedelta(days=i) for i in range(1, 2)} | {date(2026, 10, 14 + i) for i in range(5)}
        r = RulesConfig(max_training_days_per_week=6)
        ev = evaluate_day(DAY, [mk(1)], ctx(training_days_this_week=week), r, TZ)
        self.assertIn(RejectReason.WEEKLY_FREQUENCY_EXCEEDED, ev.verdicts[0].reasons)

    def test_daily_volume_limit(self):
        r = RulesConfig(max_sets_per_day=4)
        ev = evaluate_day(DAY, [mk(i, minute=i) for i in range(1, 7)], ctx(), r, TZ)
        self.assertEqual(ev.valid_sets, 4)
        self.assertEqual(ev.verdicts[-1].reasons, [RejectReason.SESSION_VOLUME_EXCEEDED])

    def test_no_muscle_data(self):
        ev = evaluate_day(DAY, [mk(1, muscles=())], ctx(), RulesConfig(), TZ)
        self.assertIn(RejectReason.NO_MUSCLE_DATA, ev.verdicts[0].reasons)


class ClientRefTest(unittest.TestCase):
    def test_deterministic_and_order_independent(self):
        a = make_client_ref(50, DAY, [3, 1, 2])
        self.assertEqual(a, make_client_ref(50, DAY, [1, 2, 3]))
        self.assertNotEqual(a, make_client_ref(50, DAY, [1, 2, 4]))
        self.assertNotEqual(a, make_client_ref(51, DAY, [1, 2, 3]))


if __name__ == "__main__":
    unittest.main()
