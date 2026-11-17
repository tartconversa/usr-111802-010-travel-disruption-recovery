"""本地演练入口：导入一份样本并查看现有业务摘要。"""
import argparse
import json
from pathlib import Path
from .service import AnalyticsService

def main():
    parser = argparse.ArgumentParser(description="运营分析服务")
    parser.add_argument("--database", required=True)
    parser.add_argument("--fixture", default="fixtures/sample.json")
    args = parser.parse_args()
    sample = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    service = AnalyticsService(args.database)
    try:
        for subject in sample["entities"]:
            service.register(**subject)
        for alias in sample["aliases"]:
            service.link(**alias)
        for event in sample["events"]:
            service.ingest(event)
        print(json.dumps(service.summary(), ensure_ascii=False, sort_keys=True))
    finally:
        service.close()

if __name__ == "__main__":
    main()
