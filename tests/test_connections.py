"""核验联程衔接风险分析、处置清单快照与重新通知批次。"""
import tempfile
import unittest
from pathlib import Path
from analytics import AnalyticsService


def at(clock):
    return f"2026-11-01T{clock}:00+08:00"


def schedule(event_id, revision, train, station, observed, arrival=None, departure=None, seats=None):
    payload = {"train": train, "station": station}
    if arrival is not None:
        payload["arrival"] = arrival
    if departure is not None:
        payload["departure"] = departure
    if seats is not None:
        payload["seats"] = seats
    return {"source": "ops", "event_id": event_id, "revision": revision, "kind": "schedule",
            "entity": train, "occurred_at": observed, "observed_at": observed, "payload": payload}


def ticket(event_id, entity, journey, observed, source="system"):
    return {"source": source, "event_id": event_id, "revision": 1, "kind": "ticket",
            "entity": entity, "occurred_at": observed, "observed_at": observed,
            "payload": {"journey": journey}}


def rebook(event_id, entity, journey, observed, revision=1, deleted=False):
    return {"source": "system", "event_id": event_id, "revision": revision, "kind": "rebook",
            "entity": entity, "occurred_at": observed, "observed_at": observed,
            "payload": {"journey": journey}, "deleted": deleted}


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = AnalyticsService(Path(self.tmp.name) / "data.sqlite3")
        for index in (1, 2, 3):
            self.service.register(f"entity-{index}", f"旅客{index}", "group-1")
            self.service.link("system", f"ext-{index}", f"entity-{index}")
        self.service.add_leg("J1", 1, "G1", "上海")
        self.service.add_leg("J1", 2, "G2", "南京")
        self.service.set_interchange("南京", 10)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def break_connection(self):
        self.service.ingest(schedule("g1@nj", 1, "G1", "南京", at("08:00"), arrival=at("10:05")))
        self.service.ingest(schedule("g2@nj", 1, "G2", "南京", at("08:00"), departure=at("10:10")))

    def test_broken_connection_counts_passenger_once(self):
        self.break_connection()
        self.service.ingest(ticket("t1", "ext-1", "J1", at("08:05")))
        self.service.ingest(ticket("t2", "ext-1", "J1", at("08:06")))
        self.service.link("other", "ext-1b", "entity-1")
        self.service.ingest(ticket("t3", "ext-1b", "J1", at("08:07"), source="other"))
        # 同一旅客的另一段联程也中断，仍只计一名滞留旅客
        self.service.add_leg("J2", 1, "G5", "上海")
        self.service.add_leg("J2", 2, "G6", "杭州")
        self.service.set_interchange("杭州", 10)
        self.service.ingest(schedule("g5@hz", 1, "G5", "杭州", at("08:00"), arrival=at("11:00")))
        self.service.ingest(schedule("g6@hz", 1, "G6", "杭州", at("08:00"), departure=at("11:05")))
        self.service.ingest(ticket("t4", "ext-1", "J2", at("08:08")))
        result = self.service.assess_connections()
        self.assertEqual(result["totals"]["needs_handling_passengers"], 1)
        self.assertEqual(result["totals"]["needs_handling_cases"], 2)
        self.assertEqual(len(result["cases"]), 2)
        first = result["cases"][0]
        self.assertEqual(first["passenger"], "entity-1")
        self.assertEqual(first["status"], "needs_handling")
        conn = first["connections"][0]
        self.assertEqual(conn["status"], "broken")
        self.assertEqual(conn["slack_minutes"], -5)
        self.assertEqual(conn["triggers"], [{"kind": "planned_insufficient"}])
        self.assertEqual(result["totals"]["broken_connections"], 2)

    def test_schedule_revision_triggers_risk(self):
        self.service.ingest(schedule("g1@nj", 1, "G1", "南京", at("08:00"), arrival=at("10:00")))
        self.service.ingest(schedule("g2@nj", 1, "G2", "南京", at("08:00"), departure=at("10:20")))
        self.service.ingest(ticket("t1", "ext-1", "J1", at("08:05")))
        self.assertEqual(self.service.assess_connections()["cases"], [])
        # 前段到达改点延后，衔接中断
        self.service.ingest(schedule("g1@nj", 2, "G1", "南京", at("09:00"), arrival=at("10:15")))
        result = self.service.assess_connections()
        conn = result["cases"][0]["connections"][0]
        self.assertEqual(conn["slack_minutes"], -5)
        self.assertEqual(len(conn["triggers"]), 1)
        trigger = conn["triggers"][0]
        self.assertEqual(trigger["kind"], "arrival_delayed")
        self.assertEqual(trigger["train"], "G1")
        self.assertEqual(trigger["from"], at("10:00"))
        self.assertEqual(trigger["to"], at("10:15"))
        self.assertEqual(trigger["event"]["revision"], 2)
        # 回到改点前的观察时刻，衔接仍然正常
        prior = self.service.assess_connections(at("08:30"))
        self.assertEqual(prior["cases"], [])

    def test_departure_advanced_triggers_risk(self):
        self.service.ingest(schedule("g1@nj", 1, "G1", "南京", at("08:00"), arrival=at("10:00")))
        self.service.ingest(schedule("g2@nj", 1, "G2", "南京", at("08:00"), departure=at("10:20")))
        self.service.ingest(ticket("t1", "ext-1", "J1", at("08:05")))
        # 后段发车改点提前，衔接中断
        self.service.ingest(schedule("g2@nj", 2, "G2", "南京", at("09:00"), departure=at("10:05")))
        conn = self.service.assess_connections()["cases"][0]["connections"][0]
        self.assertEqual(conn["slack_minutes"], -5)
        self.assertEqual(conn["triggers"][0]["kind"], "departure_advanced")
        self.assertEqual(conn["triggers"][0]["train"], "G2")

    def test_interchange_minimum_boundary(self):
        self.service.ingest(schedule("g1@nj", 1, "G1", "南京", at("08:00"), arrival=at("10:00")))
        self.service.ingest(schedule("g2@nj", 1, "G2", "南京", at("08:00"), departure=at("10:10")))
        self.service.ingest(ticket("t1", "ext-1", "J1", at("08:05")))
        # 余量恰好等于最短换乘时间，衔接可行
        self.assertEqual(self.service.assess_connections()["cases"], [])
        self.service.ingest(schedule("g2@nj", 2, "G2", "南京", at("09:00"), departure=at("10:09")))
        conn = self.service.assess_connections()["cases"][0]["connections"][0]
        self.assertEqual(conn["slack_minutes"], -1)

    def test_rebook_marks_case_handled(self):
        self.break_connection()
        self.service.ingest(ticket("t1", "ext-1", "J1", at("08:05")))
        self.assertEqual(self.service.assess_connections()["totals"]["needs_handling_passengers"], 1)
        self.service.ingest(rebook("rb1", "ext-1", "J1", at("09:00")))
        result = self.service.assess_connections()
        self.assertEqual(result["totals"]["needs_handling_passengers"], 0)
        self.assertEqual(result["totals"]["handled_passengers"], 1)
        case = result["cases"][0]
        self.assertEqual(case["status"], "handled")
        self.assertEqual(case["rebook"]["event_id"], "rb1")
        # 已处置旅客不占替代席位核对范围
        self.assertEqual(result["connections"], [])
        # 改签被撤回后重新进入待处置
        self.service.ingest(rebook("rb1", "ext-1", "J1", at("09:30"), revision=2, deleted=True))
        self.assertEqual(self.service.assess_connections()["totals"]["needs_handling_passengers"], 1)

    def test_alternative_seat_coverage(self):
        self.break_connection()
        for index in (1, 2, 3):
            self.service.ingest(ticket(f"t{index}", f"ext-{index}", "J1", at("08:05")))
        # 发车早于可衔接时刻的车次不计入替代
        self.service.ingest(schedule("g8@nj", 1, "G8", "南京", at("08:00"), departure=at("10:12"), seats=50))
        self.service.ingest(schedule("g9@nj", 1, "G9", "南京", at("08:00"), departure=at("10:40"), seats=2))
        result = self.service.assess_connections()
        self.assertEqual(len(result["connections"]), 1)
        conn = result["connections"][0]
        self.assertEqual([item["train"] for item in conn["alternatives"]], ["G9"])
        self.assertEqual(conn["seats_total"], 2)
        self.assertFalse(conn["covered"])
        self.assertEqual(conn["shortfall"], 1)
        self.assertEqual(conn["passengers"], ["entity-1", "entity-2", "entity-3"])
        case = result["cases"][0]
        self.assertFalse(case["connections"][0]["coverage"]["covered"])
        # 席位修订补足后覆盖
        self.service.ingest(schedule("g9@nj", 2, "G9", "南京", at("09:00"), departure=at("10:40"), seats=3))
        conn = self.service.assess_connections()["connections"][0]
        self.assertTrue(conn["covered"])
        self.assertEqual(conn["shortfall"], 0)

    def test_published_list_retains_basis(self):
        self.service.ingest(schedule("g1@nj", 1, "G1", "南京", at("08:00"), arrival=at("10:00")))
        self.service.ingest(schedule("g2@nj", 1, "G2", "南京", at("08:00"), departure=at("10:20")))
        self.service.ingest(ticket("t1", "ext-1", "J1", at("08:05")))
        self.service.ingest(schedule("g1@nj", 2, "G1", "南京", at("09:00"), arrival=at("10:15")))
        record = self.service.publish_handling_list("list-1", at("09:30"))
        self.assertEqual(record["kind"], "handling-list")
        self.assertEqual(record["title"], "衔接风险处置清单")
        body = record["body"]
        self.assertEqual(body["as_of"], at("09:00"))
        basis = body["basis"]
        schedule_basis = {item["train"]: item for item in basis["schedule"]}
        self.assertEqual(schedule_basis["G1"]["event"]["revision"], 2)
        self.assertEqual(schedule_basis["G1"]["baseline"]["revision"], 1)
        self.assertEqual(basis["interchange"], {"南京": 10})
        self.assertEqual([leg["train"] for leg in basis["journeys"]["J1"]], ["G1", "G2"])
        self.assertEqual([item["event"]["event_id"] for item in basis["tickets"]], ["t1"])
        # 同编号同内容幂等，同编号不同内容拒绝
        same = self.service.publish_handling_list("list-1", at("09:30"))
        self.assertEqual(same, record)
        self.service.ingest(schedule("g1@nj", 3, "G1", "南京", at("10:00"), arrival=at("10:25")))
        with self.assertRaises(ValueError):
            self.service.publish_handling_list("list-1", at("10:30"))
        # 新运行消息到达后，已下发清单及其依据保持不变
        kept = self.service.decisions.get("list-1")
        self.assertEqual(kept, record)
        kept_basis = {item["train"]: item for item in kept["body"]["basis"]["schedule"]}
        self.assertEqual(kept_basis["G1"]["event"]["revision"], 2)

    def test_handling_delta_batches(self):
        self.service.ingest(schedule("g1@nj", 1, "G1", "南京", at("08:00"), arrival=at("10:00")))
        self.service.ingest(schedule("g2@nj", 1, "G2", "南京", at("08:00"), departure=at("10:20")))
        self.service.ingest(ticket("t1", "ext-1", "J1", at("08:05")))
        self.service.ingest(ticket("t2", "ext-2", "J1", at("08:05")))
        self.service.publish_handling_list("list-1", at("08:30"))
        # 前段改点：两名旅客进入清单，属于首次通知
        self.service.ingest(schedule("g1@nj", 2, "G1", "南京", at("09:00"), arrival=at("10:15")))
        delta = self.service.handling_delta()
        self.assertEqual(delta["basis_list"]["list_id"], "list-1")
        self.assertEqual([b["passenger"] for b in delta["batches"]["new"]], ["entity-1", "entity-2"])
        self.assertEqual(delta["batches"]["changed"], [])
        self.assertEqual(delta["batches"]["cleared"], [])
        self.service.publish_handling_list("list-2", at("09:30"))
        # 通用报告不干扰处置清单基线
        self.service.publish("monthly-1", at("09:35"), "月度观察")
        # 进一步改点：风险依据变化，需要重新通知
        self.service.ingest(schedule("g1@nj", 3, "G1", "南京", at("10:00"), arrival=at("10:25")))
        delta = self.service.handling_delta()
        self.assertEqual(delta["basis_list"]["list_id"], "list-2")
        self.assertEqual([b["passenger"] for b in delta["batches"]["changed"]], ["entity-1", "entity-2"])
        self.assertEqual(delta["batches"]["changed"][0]["before"]["worst_slack_minutes"], -5)
        self.assertEqual(delta["batches"]["new"], [])
        self.service.publish_handling_list("list-3", at("10:30"))
        # 旅客二改签：进入解除批次；旅客一情况未变，无需重复通知
        self.service.ingest(rebook("rb1", "ext-2", "J1", at("10:35")))
        delta = self.service.handling_delta()
        self.assertEqual(delta["basis_list"]["list_id"], "list-3")
        self.assertEqual(delta["batches"]["cleared"],
                         [{"passenger": "entity-2", "journey": "J1", "reason": "rebooked"}])
        self.assertEqual([b["passenger"] for b in delta["batches"]["unchanged"]], ["entity-1"])
        self.assertEqual(delta["current"]["totals"]["needs_handling_passengers"], 1)
        # 新旅客购票进入清单；衔接恢复后剩余旅客解除
        self.service.ingest(ticket("t3", "ext-3", "J1", at("10:40")))
        delta = self.service.handling_delta()
        self.assertEqual([b["passenger"] for b in delta["batches"]["new"]], ["entity-3"])
        self.service.ingest(schedule("g1@nj", 4, "G1", "南京", at("11:00"), arrival=at("10:00")))
        delta = self.service.handling_delta()
        cleared = {b["passenger"]: b["reason"] for b in delta["batches"]["cleared"]}
        self.assertEqual(cleared, {"entity-1": "feasible", "entity-2": "feasible"})
        self.assertEqual(delta["batches"]["new"], [])
        self.assertEqual(delta["batches"]["unchanged"], [])

    def test_delta_without_published_list(self):
        self.break_connection()
        self.service.ingest(ticket("t1", "ext-1", "J1", at("08:05")))
        delta = self.service.handling_delta()
        self.assertIsNone(delta["basis_list"])
        self.assertEqual([b["passenger"] for b in delta["batches"]["new"]], ["entity-1"])

    def test_unverifiable_connections(self):
        self.service.ingest(ticket("t1", "ext-1", "J1", at("08:05")))
        self.service.ingest(ticket("t2", "ext-2", "J9", at("08:05")))
        self.service.add_leg("J2", 1, "G3", "上海")
        self.service.add_leg("J2", 2, "G4", "杭州")
        self.service.ingest(schedule("g3@hz", 1, "G3", "杭州", at("08:00"), arrival=at("10:00")))
        self.service.ingest(schedule("g4@hz", 1, "G4", "杭州", at("08:00"), departure=at("10:05")))
        self.service.ingest(ticket("t3", "ext-3", "J2", at("08:05")))
        result = self.service.assess_connections()
        self.assertEqual(result["cases"], [])
        reasons = {}
        for item in result["unverifiable"]:
            reasons[item["journey"]] = item["reason"] or item["connections"][0]["reason"]
        self.assertEqual(reasons, {"J1": "missing_arrival_schedule",
                                   "J9": "journey_unknown",
                                   "J2": "missing_interchange_minutes"})
        self.assertEqual(result["totals"]["unverifiable_passengers"], 3)

    def test_invalid_payloads_are_skipped(self):
        bad_ticket = ticket("t-bad", "ext-1", "J1", at("08:05"))
        del bad_ticket["payload"]["journey"]
        self.service.ingest(bad_ticket)
        self.service.ingest(schedule("s-bad", 1, "G1", "", at("08:00"), arrival=at("10:00")))
        self.service.ingest(schedule("s-bad2", 1, "G1", "南京", at("08:00"), arrival="not-a-time"))
        self.service.ingest(schedule("s-bad3", 1, "G1", "南京", at("08:00"), departure=at("10:00"), seats=-1))
        bad_rebook = rebook("r-bad", "ext-1", "J1", at("08:00"))
        del bad_rebook["payload"]["journey"]
        self.service.ingest(bad_rebook)
        result = self.service.assess_connections()
        reasons = sorted(item["reason"] for item in result["skipped"])
        self.assertEqual(reasons, ["rebook_missing_journey", "schedule_invalid_seats",
                                   "schedule_invalid_time", "schedule_missing_identity",
                                   "ticket_missing_journey"])
        self.assertEqual(result["totals"]["ticketed_passengers"], 0)


if __name__ == "__main__":
    unittest.main()
