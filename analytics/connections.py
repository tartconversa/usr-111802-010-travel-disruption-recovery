"""联程衔接风险分析。

把车票事件按旅客与行程关联，结合最新运行时刻修订、站内最短换乘时间和
改签状态，找出真正来不及换乘且仍需处置的旅客，并核对替代席位是否覆盖。

口径约定：
- 行程目录中每条 leg 的车站为该段乘车站；第 k 段的到达站即第 k+1 段的
  乘车站，换乘发生在下一段的乘车站。
- 旅客身份先经主体映射解析，未映射的按 “来源/外部标识” 兜底；同一旅客
  多张联程票只计一名滞留旅客。
- 同一车次同一车站的最新修订为当前时刻，最早观察到的修订为基线；衔接
  中断的触发归因来自当前时刻相对基线的不利移动。
- 替代席位取同一换乘站、发车不早于 “前段到达 + 最短换乘” 的其他车次
  （不含前段到达车次与错过车次）的 seats 合计，与仍需处置旅客数对比。
"""
from collections import defaultdict
from datetime import datetime, timedelta
from .store import timestamp


def _moment(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _minutes(seconds):
    value = seconds / 60
    return int(value) if value.is_integer() else value


def _event_ref(event):
    return {"source": event["source"], "event_id": event["event_id"],
            "revision": event["revision"], "observed_at": event["observed_at"]}


def _ref_order(ref):
    return (_moment(ref["observed_at"]), ref["source"], ref["event_id"], ref["revision"])


def _valid_text(value):
    return isinstance(value, str) and bool(value.strip())


class ConnectionRisk:
    """按当前事件修订评估联程衔接风险。"""

    def __init__(self, store, entities, directory, kinds=None):
        self.store = store
        self.entities = entities
        self.directory = directory
        kinds = kinds or {}
        self.ticket_kind = kinds.get("ticket_kind", "ticket")
        self.schedule_kind = kinds.get("schedule_kind", "schedule")
        self.rebook_kind = kinds.get("rebook_kind", "rebook")

    def _passenger(self, event):
        resolved = self.entities.resolve(event["source"], event["entity"])
        return resolved if resolved else f"{event['source']}/{event['entity']}"

    def _skip(self, skipped, event, reason):
        if skipped is not None:
            skipped.append(_event_ref(event) | {"kind": event["kind"], "reason": reason})

    def _schedule_entry(self, event, skipped):
        payload = event["payload"]
        train, station = payload.get("train"), payload.get("station")
        if not (_valid_text(train) and _valid_text(station)):
            self._skip(skipped, event, "schedule_missing_identity")
            return None
        arrival = payload.get("arrival")
        if arrival is not None:
            moment = _moment(arrival)
            if moment is None:
                self._skip(skipped, event, "schedule_invalid_time")
                return None
            arrival = moment.isoformat()
        departure = payload.get("departure")
        if departure is not None:
            moment = _moment(departure)
            if moment is None:
                self._skip(skipped, event, "schedule_invalid_time")
                return None
            departure = moment.isoformat()
        seats = payload.get("seats")
        if seats is not None and (type(seats) is not int or seats < 0):
            self._skip(skipped, event, "schedule_invalid_seats")
            return None
        return {"train": train, "station": station, "arrival": arrival,
                "departure": departure, "seats": seats, "event": _event_ref(event)}

    def _journey_key(self, event, skipped, missing_reason):
        journey = event["payload"].get("journey")
        if not _valid_text(journey):
            self._skip(skipped, event, missing_reason)
            return None
        return (self._passenger(event), journey)

    def _triggers(self, train, next_train, station, arrival_entry, departure_entry, baseline):
        triggers = []
        base_arrival = baseline.get((train, station))
        if (base_arrival and base_arrival["arrival"] and arrival_entry["arrival"]
                and _moment(arrival_entry["arrival"]) > _moment(base_arrival["arrival"])):
            triggers.append({"kind": "arrival_delayed", "train": train,
                             "from": base_arrival["arrival"], "to": arrival_entry["arrival"],
                             "event": arrival_entry["event"]})
        base_departure = baseline.get((next_train, station))
        if (base_departure and base_departure["departure"] and departure_entry["departure"]
                and _moment(departure_entry["departure"]) < _moment(base_departure["departure"])):
            triggers.append({"kind": "departure_advanced", "train": next_train,
                             "from": base_departure["departure"], "to": departure_entry["departure"],
                             "event": departure_entry["event"]})
        if not triggers:
            triggers.append({"kind": "planned_insufficient"})
        return triggers

    def _evaluate(self, leg_no, train, next_no, next_train, station,
                  current, baseline, used_stations, used_schedule):
        used_stations.add(station)
        arrival_entry = current.get((train, station))
        departure_entry = current.get((next_train, station))
        if arrival_entry is not None:
            used_schedule.add((train, station))
        if departure_entry is not None:
            used_schedule.add((next_train, station))
        base = {"leg_from": leg_no, "leg_to": next_no, "station": station,
                "arrival_train": train, "departure_train": next_train}
        if arrival_entry is None or arrival_entry["arrival"] is None:
            return base | {"status": "unknown", "reason": "missing_arrival_schedule"}
        if departure_entry is None or departure_entry["departure"] is None:
            return base | {"status": "unknown", "reason": "missing_departure_schedule"}
        result = base | {"arrival": arrival_entry["arrival"],
                         "departure": departure_entry["departure"],
                         "arrival_event": arrival_entry["event"],
                         "departure_event": departure_entry["event"]}
        min_transfer = self.directory.interchange_for(station)
        if min_transfer is None:
            return result | {"status": "unknown", "reason": "missing_interchange_minutes"}
        slack = (_moment(result["departure"]) - _moment(result["arrival"])).total_seconds() - min_transfer * 60
        result["min_transfer_minutes"] = min_transfer
        result["slack_minutes"] = _minutes(slack)
        if slack >= 0:
            return result | {"status": "ok"}
        result["status"] = "broken"
        result["triggers"] = self._triggers(train, next_train, station,
                                            arrival_entry, departure_entry, baseline)
        return result

    @staticmethod
    def _basis_schedule(key, current, baseline):
        entry = current[key]
        base = baseline.get(key)
        return {"train": entry["train"], "station": entry["station"],
                "arrival": entry["arrival"], "departure": entry["departure"],
                "seats": entry["seats"], "event": entry["event"],
                "baseline": base["event"] if base else None}

    def assess(self, as_of=None):
        """评估截至某时刻的衔接风险；as_of 为空时钉在最新观察时间。"""
        pinned = timestamp(as_of) if as_of is not None else None
        skipped = []
        schedule_current = {}
        tickets = defaultdict(list)
        rebooks = {}
        latest_observed = None
        for event in self.store.current(pinned):
            moment = _moment(event["observed_at"])
            if latest_observed is None or moment > latest_observed:
                latest_observed = moment
            kind = event["kind"]
            if kind == self.schedule_kind:
                entry = self._schedule_entry(event, skipped)
                if entry is None:
                    continue
                key = (entry["train"], entry["station"])
                prior = schedule_current.get(key)
                if prior is None or _ref_order(entry["event"]) >= _ref_order(prior["event"]):
                    schedule_current[key] = entry
            elif kind == self.ticket_kind:
                key = self._journey_key(event, skipped, "ticket_missing_journey")
                if key is not None:
                    tickets[key].append(_event_ref(event))
            elif kind == self.rebook_kind:
                key = self._journey_key(event, skipped, "rebook_missing_journey")
                if key is None:
                    continue
                ref = _event_ref(event)
                prior = rebooks.get(key)
                if prior is None or _ref_order(ref) >= _ref_order(prior):
                    rebooks[key] = ref
        if pinned is None and latest_observed is not None:
            pinned = latest_observed.isoformat()

        schedule_baseline = {}
        for event in self.store.history(pinned):
            if event["kind"] != self.schedule_kind:
                continue
            entry = self._schedule_entry(event, None)
            if entry is None:
                continue
            key = (entry["train"], entry["station"])
            prior = schedule_baseline.get(key)
            if prior is None or _ref_order(entry["event"]) < _ref_order(prior["event"]):
                schedule_baseline[key] = entry

        cases = []
        unverifiable = []
        used_journeys = {}
        used_stations = set()
        used_schedule = set()
        for passenger, journey in sorted(tickets):
            legs = self.directory.legs(journey)
            if not legs:
                unverifiable.append({"passenger": passenger, "journey": journey,
                                     "reason": "journey_unknown", "connections": []})
                continue
            used_journeys[journey] = legs
            evals = []
            for index in range(len(legs) - 1):
                leg_no, train, _ = legs[index]
                next_no, next_train, station = legs[index + 1]
                evals.append(self._evaluate(leg_no, train, next_no, next_train, station,
                                            schedule_current, schedule_baseline,
                                            used_stations, used_schedule))
            broken = [item for item in evals if item["status"] == "broken"]
            unknown = [item for item in evals if item["status"] == "unknown"]
            if broken:
                rebook = rebooks.get((passenger, journey))
                cases.append({"passenger": passenger, "journey": journey,
                              "status": "handled" if rebook else "needs_handling",
                              "rebook": rebook,
                              "worst_slack_minutes": min(item["slack_minutes"] for item in broken),
                              "connections": evals})
            elif unknown:
                unverifiable.append({"passenger": passenger, "journey": journey,
                                     "reason": None, "connections": unknown})

        # 同一物理换乘（同站同前后车次）合并，核对替代席位对仍需处置旅客的覆盖。
        groups = {}
        for case in cases:
            if case["status"] != "needs_handling":
                continue
            for conn in case["connections"]:
                if conn["status"] != "broken":
                    continue
                key = (conn["station"], conn["arrival_train"], conn["departure_train"])
                group = groups.setdefault(key, {"connection": conn, "passengers": set()})
                group["passengers"].add(case["passenger"])
        connections = []
        for (station, arrival_train, departure_train), group in sorted(groups.items()):
            conn = group["connection"]
            threshold = _moment(conn["arrival"]) + timedelta(minutes=conn["min_transfer_minutes"])
            alternatives = []
            for (train, place), entry in schedule_current.items():
                if place != station or train in (arrival_train, departure_train):
                    continue
                if entry["departure"] is None or _moment(entry["departure"]) < threshold:
                    continue
                alternatives.append({"train": train, "departure": entry["departure"],
                                     "seats": entry["seats"], "event": entry["event"]})
                used_schedule.add((train, place))
            alternatives.sort(key=lambda item: (_moment(item["departure"]), item["train"]))
            passengers = sorted(group["passengers"])
            seats_total = sum(item["seats"] for item in alternatives if item["seats"] is not None)
            connections.append({"station": station, "arrival_train": arrival_train,
                                "departure_train": departure_train,
                                "arrival": conn["arrival"], "departure": conn["departure"],
                                "min_transfer_minutes": conn["min_transfer_minutes"],
                                "slack_minutes": conn["slack_minutes"],
                                "triggers": conn["triggers"],
                                "passengers": passengers, "alternatives": alternatives,
                                "seats_total": seats_total,
                                "covered": seats_total >= len(passengers),
                                "shortfall": max(0, len(passengers) - seats_total)})
        coverage = {(item["station"], item["arrival_train"], item["departure_train"]): item
                    for item in connections}
        for case in cases:
            if case["status"] != "needs_handling":
                continue
            for conn in case["connections"]:
                if conn["status"] != "broken":
                    continue
                key = (conn["station"], conn["arrival_train"], conn["departure_train"])
                info = coverage[key]
                conn["coverage"] = {"covered": info["covered"], "seats_total": info["seats_total"],
                                    "shortfall": info["shortfall"], "passengers": info["passengers"]}

        cases.sort(key=lambda case: (0 if case["status"] == "needs_handling" else 1,
                                     case["worst_slack_minutes"],
                                     case["passenger"], case["journey"]))
        unverifiable.sort(key=lambda item: (item["passenger"], item["journey"]))
        at_risk = {case["passenger"] for case in cases}
        pending = {case["passenger"] for case in cases if case["status"] == "needs_handling"}
        totals = {
            "ticketed_passengers": len({passenger for passenger, _ in tickets}),
            "at_risk_passengers": len(at_risk),
            "needs_handling_passengers": len(pending),
            "handled_passengers": len(at_risk - pending),
            "unverifiable_passengers": len({item["passenger"] for item in unverifiable}),
            "needs_handling_cases": sum(1 for case in cases if case["status"] == "needs_handling"),
            "broken_connections": len(connections),
        }
        basis = {
            "journeys": {journey: [{"leg_no": leg_no, "train": train, "station": station}
                                   for leg_no, train, station in used_journeys[journey]]
                         for journey in sorted(used_journeys)},
            "interchange": {station: self.directory.interchange_for(station)
                            for station in sorted(used_stations)},
            "schedule": [self._basis_schedule(key, schedule_current, schedule_baseline)
                         for key in sorted(used_schedule)],
            "tickets": [{"passenger": passenger, "journey": journey, "event": ref}
                        for (passenger, journey), refs in sorted(tickets.items())
                        for ref in sorted(refs, key=_ref_order)],
            "rebooks": [{"passenger": passenger, "journey": journey, "event": ref}
                        for (passenger, journey), ref in sorted(rebooks.items())],
        }
        return {"as_of": pinned, "cases": cases, "connections": connections,
                "unverifiable": unverifiable, "skipped": skipped,
                "totals": totals, "basis": basis}


def _needs_handling(analysis):
    return {(case["passenger"], case["journey"]): case
            for case in analysis.get("cases", []) if case["status"] == "needs_handling"}


def _signature(case):
    parts = []
    for conn in case["connections"]:
        if conn["status"] != "broken":
            continue
        coverage = conn.get("coverage") or {}
        parts.append((conn["station"], conn["arrival_train"], conn["departure_train"],
                      conn["arrival"], conn["departure"], conn["slack_minutes"],
                      coverage.get("covered")))
    return tuple(sorted(parts))


def _brief(case):
    return {"passenger": case["passenger"], "journey": case["journey"],
            "worst_slack_minutes": case["worst_slack_minutes"],
            "connections": [conn for conn in case["connections"] if conn["status"] == "broken"]}


def notification_batches(published, current):
    """对比已下发清单与当前评估，给出需要重新通知的旅客批次。

    published 为已下发清单的正文（没有时为 None），current 为当前评估。
    返回 new/changed/cleared/unchanged 四批；cleared 的 reason 取值：
    rebooked（已改签）、feasible（衔接恢复）、unverifiable（数据不足）、
    ticket_withdrawn（车票失效）。
    """
    previous = _needs_handling(published) if published else {}
    pending = _needs_handling(current)
    batches = {"new": [], "changed": [], "cleared": [], "unchanged": []}
    for key in sorted(pending):
        case = pending[key]
        prior = previous.get(key)
        if prior is None:
            batches["new"].append(_brief(case))
        elif _signature(prior) != _signature(case):
            batches["changed"].append(_brief(case) | {"before": _brief(prior)})
        else:
            batches["unchanged"].append(_brief(case))
    ticketed = {(item["passenger"], item["journey"]) for item in current["basis"]["tickets"]}
    for key in sorted(previous):
        if key in pending:
            continue
        if any((case["passenger"], case["journey"]) == key for case in current["cases"]):
            reason = "rebooked"
        elif any((item["passenger"], item["journey"]) == key for item in current["unverifiable"]):
            reason = "unverifiable"
        elif key not in ticketed:
            reason = "ticket_withdrawn"
        else:
            reason = "feasible"
        batches["cleared"].append({"passenger": key[0], "journey": key[1], "reason": reason})
    return batches
