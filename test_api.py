"""HTTP API 端到端测试：角色、赛区隔离、幂等头、掩码视图。"""

import hashlib
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from api import ApiHandler
from app import Application
from test_helpers import SHA


def make_server(app):
    handler = type("BoundHandler", (ApiHandler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.app = Application()
        self.server, self.thread = make_server(self.app)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, body=None, *, role=None, station=None,
             user=None, idem=None, expect=None):
        headers = {"Content-Type": "application/json"}
        if role:
            headers["X-Role"] = role
        if station:
            headers["X-Station"] = station
        if user:
            headers["X-User"] = user
        if idem:
            headers["Idempotency-Key"] = idem
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") \
            if body is not None else None
        req = Request(self.base + quote(path, safe="/?=&"), data=data, headers=headers,
                      method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                payload = json.load(resp)
                if expect:
                    self.assertEqual(resp.status, expect)
                return resp.status, payload
        except HTTPError as exc:
            payload = json.load(exc)
            if expect:
                self.assertEqual(exc.code, expect, payload)
            return exc.code, payload

    def test_health_and_contract(self):
        _, health = self.call("GET", "/health")
        self.assertEqual(health["status"], "ok")
        _, contract = self.call("GET", "/contract")
        self.assertGreaterEqual(len(contract["invariants"]), 3)

    def test_role_required(self):
        status, body = self.call("POST", "/stations/freeze", {})
        self.assertEqual(status, 401)

    def test_full_tournament_flow_over_http(self):
        rules = {"station_id": "east", "rules_version": "http-r1",
                 "groups": [{"name": "名手组", "quota": 1}],
                 "rounds": 1, "tiebreak": ["对手分总和", "抽签"]}
        self.call("POST", "/stations/freeze", rules,
                  role="station-admin", station="east", user="admin",
                  idem="fz-east", expect=201)
        # 跨赛区管理员不能冻别人的站（scope 只在具体站点路由校验，freeze 是全局操作）
        docs = [["张三", "11010119900101123X"], ["李四", "11010219900202456X"]]
        pids = []
        for i, (name, doc) in enumerate(docs):
            _, body = self.call(
                "POST", "/stations/east/register",
                {"group": "名手组", "name": name,
                 "documents": [{"type": "身份证", "number": doc}]},
                role="station-admin", station="east", user="admin",
                idem=f"reg-{i}", expect=201)
            pids.append(body["person_id"])
        # 西赛区角色不能读东赛区名单
        status, _ = self.call("GET", "/stations/east/roster-view",
                              role="station-admin", station="west", expect=403)
        # 本赛区视角是掩码的
        _, view = self.call("GET", "/stations/east/roster-view",
                            role="station-admin", station="east")
        self.assertEqual(len(view["persons"]), 2)
        self.assertNotIn("11010119900101123X", json.dumps(view, ensure_ascii=False))

        _, pair = self.call("POST", "/stations/east/rounds/1/pairings", {},
                            role="judge", station="east", user="judge-zhao",
                            idem="pair-1", expect=201)
        real = next(g for g in pair["event"]["payload"]["games"]
                    if not g["is_bye"])
        result_body = {"result_type": "胜负", "outcome": "黑胜",
                       "record_hash": SHA("g1"), "record_kind": "棋谱",
                       "signature": {"signee": "赵裁判", "role": "裁判",
                                     "source": "现场签字单"}}
        _, r1 = self.call(
            "POST", f"/stations/east/games/{real['game_id']}/result",
            result_body, role="judge", station="east", user="judge-zhao",
            idem="res-1", expect=201)
        self.assertEqual(r1["effective"]["current"]["outcome"], "黑胜")
        # 非裁判不能提交临场成绩
        self.call("POST", f"/stations/east/games/{real['game_id']}/result",
                  result_body, role="station-admin", station="east",
                  user="admin", idem="res-admin", expect=403)
        # 重投同幂等键：同一事实
        _, r2 = self.call(
            "POST", f"/stations/east/games/{real['game_id']}/result",
            result_body, role="judge", station="east", user="judge-zhao",
            idem="res-1", expect=201)
        self.assertEqual(r2["event"]["seq"], r1["event"]["seq"])
        # 纠错是新事件
        fixed = dict(result_body, outcome="白胜",
                     record_hash=SHA("g1-fix"))
        _, c1 = self.call(
            "POST", f"/stations/east/games/{real['game_id']}/correction",
            dict(fixed, reason="黑白记反"), role="station-admin",
            station="east", user="admin", idem="corr-1", expect=201)
        self.assertEqual(c1["event"]["type"], "ResultCorrected")

        self.call("POST", "/stations/east/finalize", {},
                  role="station-admin", station="east", user="admin",
                  idem="fin-east", expect=201)
        # 终赛后投影与锁定
        self.call("POST", "/admin/freeze-rules",
                  {"expected_stations": ["east"],
                   "zone_priority": ["east"]},
                  role="organizer", user="organizer", idem="fr", expect=201)
        self.call("POST", "/federation/project/east", {},
                  role="organizer", user="organizer", idem="pe", expect=201)
        _, status = self.call("GET", "/federation/status/east/名手组",
                              role="organizer", user="organizer")
        self.assertEqual(len(status["occupied"]), 1)
        self.call("POST", "/federation/lock", {},
                  role="organizer", user="organizer", idem="lock", expect=201)
        _, roster = self.call("GET", "/federation/roster",
                              role="organizer", user="organizer")
        self.assertEqual(len(roster["groups"]["名手组"]), 1)
        # 解释端点
        _, ex = self.call("GET", f"/federation/explain/{pids[0]}",
                          role="organizer", user="organizer")
        self.assertTrue(ex["participations"][0]["games"])
        self.assertEqual(ex["participations"][0]["rules_version"], "http-r1")
        # 赛区角色无法访问总决赛解释（完整身份不泄露）
        self.call("GET", f"/federation/explain/{pids[0]}",
                  role="station-admin", station="east", expect=403)

    def test_late_result_and_appeal_channels(self):
        rules = {"station_id": "north", "rules_version": "http-r2",
                 "groups": [{"name": "名人组", "quota": 1}],
                 "rounds": 1, "tiebreak": ["抽签"]}
        self.call("POST", "/stations/freeze", rules,
                  role="station-admin", user="admin", idem="fz-n")
        self.call("POST", "/stations/north/register",
                  {"group": "名人组", "name": "甲",
                   "documents": [{"type": "护照", "number": "P1"}]},
                  role="station-admin", station="north", idem="rn1")
        self.call("POST", "/stations/north/register",
                  {"group": "名人组", "name": "乙",
                   "documents": [{"type": "护照", "number": "P2"}]},
                  role="station-admin", station="north", idem="rn2")
        _, pair = self.call("POST", "/stations/north/rounds/1/pairings", {},
                            role="judge", station="north", idem="pn1")
        g = pair["event"]["payload"]["games"][0]
        body = {"result_type": "弃权", "outcome": "白弃权",
                "record_hash": SHA("bye-slip"), "record_kind": "弃权单",
                "signature": {"signee": "孙裁判", "role": "裁判",
                              "source": "弃权签字单"}}
        _, late = self.call(
            "POST", f"/stations/north/games/{g['game_id']}/late-result",
            dict(body, reason="交通拥堵迟到", accepted_by="裁判长"),
            role="judge", station="north", user="judge-sun",
            idem="late-n", expect=201)
        self.assertEqual(late["event"]["type"], "LateResultRecorded")
        _, appeal = self.call(
            "POST", f"/stations/north/games/{g['game_id']}/appeal",
            {"appeal_id": "AP-1", "action": "维持", "basis": "弃权程序合规",
             "ruled_by": "仲裁组"},
            role="station-admin", station="north", user="admin",
            idem="ap-n", expect=201)
        self.assertEqual(appeal["effective"]["appeal"]["action"], "维持")


if __name__ == "__main__":
    unittest.main()
