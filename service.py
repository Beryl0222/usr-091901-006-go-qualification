"""业余围棋晋级核算服务入口。

- python3 service.py --check              配置与事件链自检
- python3 service.py --port 8000 [--db gq.db]
    GET  /health /contract
    赛事、报名、冻结、结果/弃权/迟到/更正/申诉、确认、锁榜、解释与审计接口
    详见 api.py 与 README.md
"""

import argparse
import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from api import api_handler
from store import EventStore
from tournament.domain import TournamentService

SERVICE_ID = "go-qualification"
SERVICE_NAME = "业余围棋晋级核算"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_service(db_path: str = ":memory:") -> TournamentService:
    store = EventStore(db_path)
    return TournamentService(store)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=":memory:", help="事件库 SQLite 路径（默认纯内存）")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    contract = load_contract()
    if args.check:
        assert contract["states"] and contract["invariants"]
        if args.db != ":memory:" and Path(args.db).exists():
            result = build_service(args.db).verify()
            assert result["ok"], f"事件链校验失败: {result}"
            print(f"基础检查通过；事件链 {result['events']} 事件 / {result['streams']} 流")
        else:
            print("基础检查通过")
        return

    svc = build_service(args.db)
    handler = api_handler(svc)
    print(f"{SERVICE_NAME} 监听 0.0.0.0:{args.port}（事件库 {args.db}）")
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
