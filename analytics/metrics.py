"""提供原始事件计数和按主体汇总的现有指标。"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

class Metrics:
    def __init__(self, store, entities):
        self.store, self.entities = store, entities

    def summary(self, as_of=None):
        totals = defaultdict(lambda: {"events": 0, "amount": "0", "kinds": {}})
        unmatched = []
        for event in self.store.current(as_of):
            subject = self.entities.resolve(event["source"], event["entity"])
            if subject is None:
                unmatched.append({"source": event["source"], "event_id": event["event_id"]})
                continue
            entry = totals[subject]
            entry["events"] += 1
            kind = event["kind"]
            entry["kinds"][kind] = entry["kinds"].get(kind, 0) + 1
            try:
                amount = Decimal(str(event["payload"].get("amount", 0)))
            except InvalidOperation as exc:
                raise ValueError("金额字段无效") from exc
            if not amount.is_finite():
                raise ValueError("金额字段无效")
            entry["amount"] = str(Decimal(entry["amount"]) + amount)
        return {"subjects": dict(sorted(totals.items())), "unmatched": unmatched}
