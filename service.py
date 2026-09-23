"""业余围棋晋级核算服务入口。

* python3 service.py --check            基础配置检查
* python3 service.py --port 8000        启动赛事资格与核算 API
* python3 service.py --port 8000 --store events.jsonl
                                       启用事件日志持久化（重启完整重放）
"""

import argparse
import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from api import ApiHandler
from app import Application

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


def make_handler(app):
    """把装配好的应用注入每个请求处理器。"""
    class BoundHandler(ApiHandler):
        pass
    BoundHandler.app = app
    return BoundHandler


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--store", type=str, default=None,
                        help="事件日志 JSONL 路径；不提供则仅驻留内存")
    parser.add_argument("--secret", type=str,
                        default="go-qualification-secret",
                        help="赛区通行码 HMAC 主密钥（生产环境务必另行配置）")
    args = parser.parse_args()

    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        app = Application()  # 内存态自检：装配与订阅可用
        assert app.audit()["chain_ok"]
        print("基础检查通过")
        return

    app = Application(store_path=args.store, master_secret=args.secret)
    handler = make_handler(app)
    print(f"{SERVICE_NAME} 启动于端口 {args.port}，"
          f"已载入事件 {len(app.store)} 条，尾部 "
          f"{app.store.tail_hash[:12]}")
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
