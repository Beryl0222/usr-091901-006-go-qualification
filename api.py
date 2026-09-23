"""HTTP 适配层。

角色通过请求头声明（演示用，生产环境应接鉴权中间件）：
  X-Role: 赛区管理员|裁判|资格审核员|总决赛组委会
  X-Station: 赛区编号（赛区侧操作必填，且只能操作本赛区）
  X-User: 操作人
  Idempotency-Key: 重投同一轮成绩时复用，服务端返回首次事实
"""

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

from events import EventError

STATION_ROLES = {"赛区管理员", "裁判"}
REVIEWER_ROLES = {"资格审核员", "总决赛组委会"}

# HTTP 头只能承载 latin-1，角色用 ASCII 令牌传入，再映射为领域角色。
ROLE_TOKENS = {
    "station-admin": "赛区管理员",
    "judge": "裁判",
    "reviewer": "资格审核员",
    "organizer": "总决赛组委会",
}


class ApiError(EventError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class ApiHandler(BaseHTTPRequestHandler):
    app = None  # 由 make_server 注入

    # ---------- 基础收发 ----------
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _query(self):
        return {k: v[-1] for k, v in
                parse_qs(urlparse(self.path).query).items()}

    def log_message(self, *_args):
        return

    # ---------- 鉴权 ----------
    def _actor(self):
        token = self.headers.get("X-Role")
        if not token:
            raise ApiError(401, "缺少 X-Role 请求头")
        role = ROLE_TOKENS.get(token, token)
        if role not in STATION_ROLES | REVIEWER_ROLES:
            raise ApiError(403, f"未知角色令牌: {token}")
        station = self.headers.get("X-Station")
        if station and not all(ord(c) < 128 for c in station):
            raise ApiError(400, "X-Station 请使用赛区英文编号")
        return {"role": role,
                "station": station,
                "user": self.headers.get("X-User") or role}

    def _require_role(self, allowed):
        actor = self._actor()
        if actor["role"] not in allowed:
            raise ApiError(403, f"该操作需要角色: {sorted(allowed)}")
        return actor

    def _require_station_scope(self, station_id):
        actor = self._actor()
        if actor["role"] in STATION_ROLES:
            if actor["station"] != station_id:
                raise ApiError(403, "赛区角色只能访问本赛区数据")
        elif actor["role"] not in REVIEWER_ROLES:
            raise ApiError(403, "角色无权访问赛区数据")
        return actor

    # ---------- 路由 ----------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            path = unquote(urlparse(self.path).path).rstrip("/") or "/"
            if method == "GET" and path == "/health":
                from service import health_payload
                return self._send_json(health_payload())
            if method == "GET" and path == "/contract":
                from service import load_contract
                return self._send_json(load_contract())
            if self.app is None:
                raise ApiError(503, "应用尚未装配")

            # GET
            if method == "GET":
                return self._route_get(path)
            return self._route_post(path)
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except EventError as exc:
            self._send_json({"error": str(exc)}, 422)
        except Exception as exc:  # noqa: BLE001 - 适配层统一兜底
            self._send_json({"error": f"服务器内部错误: {exc}"}, 500)

    def _route_get(self, path):
        q = self._query()
        if path == "/audit":
            self._require_role(REVIEWER_ROLES)
            return self._send_json(self.app.audit())
        if path == "/events":
            self._require_role(REVIEWER_ROLES)
            etype = q.get("type")
            return self._send_json(
                [e.to_dict() for e in self.app.store.events(etype)])
        if path.startswith("/stations/") and path.endswith("/roster-view"):
            station_id = path.split("/")[2]
            actor = self._require_station_scope(station_id)
            return self._send_json({
                "station_id": station_id,
                "as_viewed_by": {"role": actor["role"],
                                 "station": actor["station"]},
                "persons": self.app.identity.station_view(station_id)})
        if path.startswith("/stations/") and path.endswith("/standings"):
            parts = path.split("/")
            station_id, _ = parts[2], parts[3]
            self._require_station_scope(station_id)
            data = self.app.stations.standings(
                station_id, q.get("group"),
                through_round=int(q["through_round"])
                if q.get("through_round") else None)
            return self._send_json(data)
        if path.startswith("/identity/candidates"):
            self._require_role(REVIEWER_ROLES)
            return self._send_json(
                self.app.identity.homophone_candidates(q.get("station_id")))
        if path.startswith("/federation/status/"):
            self._require_role(REVIEWER_ROLES)
            parts = path.split("/")
            return self._send_json(
                self.app.federation.promotion_status(parts[3], parts[4]))
        if path == "/federation/roster":
            self._require_role(REVIEWER_ROLES)
            return self._send_json(self.app.federation.final_roster())
        if path.startswith("/federation/explain/"):
            self._require_role(REVIEWER_ROLES)
            pid = path.split("/")[-1]
            return self._send_json(self.app.federation.explain(pid))
        if path == "/federation/notifications":
            actor = self._actor()
            if actor["role"] in STATION_ROLES:
                notes = self.app.federation.notifications(
                    station_id=actor["station"])
                data = [n for n in notes
                        if any(r["channel"] in ("station", "station_admin")
                               for r in n["recipients"])]
            elif actor["role"] in REVIEWER_ROLES:
                data = self.app.federation.notifications(
                    station_id=q.get("station_id"), group=q.get("group"))
            else:
                raise ApiError(403, "无权读取通知")
            return self._send_json(data)
        raise ApiError(404, "未知路由")

    def _route_post(self, path):
        body = self._read_json()
        idem = self.headers.get("Idempotency-Key")
        if path == "/stations/freeze":
            actor = self._require_role({"赛区管理员", "总决赛组委会"})
            ev = self.app.stations.freeze(
                body, submitted_by=actor["user"], idempotency_key=idem)
            return self._send_json({"event": ev.to_dict()}, 201)
        if path.startswith("/stations/") and path.endswith("/register"):
            station_id = path.split("/")[2]
            actor = self._require_station_scope(station_id)
            ev = self.app.identity.register(
                station_id=station_id, group=body["group"],
                name=body["name"], gender=body.get("gender"),
                birth_date=body.get("birth_date"),
                documents=body["documents"],
                submitted_by=actor["user"], idempotency_key=idem,
                source=f"报名:{station_id}")
            return self._send_json(
                {"event": ev.to_dict(),
                 "person_id": ev.payload["person_id"],
                 "matched_existing": ev.payload["matched_existing"],
                 "pass": self.app.identity.passcode(
                     ev.payload["person_id"], station_id)}, 201)
        if path == "/identity/merge":
            actor = self._require_role({"资格审核员"})
            ev = self.app.identity.confirm_merge(
                body["survivor_id"], body["merged_id"],
                reason=body.get("reason", ""),
                submitted_by=actor["user"], idempotency_key=idem)
            return self._send_json({"event": ev.to_dict()}, 201)
        if path.startswith("/stations/") and "/rounds/" in path and \
                path.endswith("/pairings"):
            parts = path.split("/")
            station_id, round_no = parts[2], int(parts[4])
            actor = self._require_station_scope(station_id)
            ev = self.app.stations.publish_pairings(
                station_id, round_no,
                games=body.get("games"),
                submitted_by=actor["user"], idempotency_key=idem)
            return self._send_json({"event": ev.to_dict()}, 201)
        if "/games/" in path and path.startswith("/stations/"):
            return self._route_game(path, body, actor=None, idem=idem)
        if path.startswith("/stations/") and path.endswith("/withdraw"):
            station_id = path.split("/")[2]
            actor = self._require_station_scope(station_id)
            ev = self.app.stations.withdraw(
                station_id, body["person_id"], body["group"],
                reason=body["reason"], from_round=body.get("from_round"),
                submitted_by=actor["user"], idempotency_key=idem)
            return self._send_json({"event": ev.to_dict()}, 201)
        if path.startswith("/stations/") and path.endswith("/finalize"):
            station_id = path.split("/")[2]
            actor = self._require_station_scope(station_id)
            ev = self.app.stations.finalize(
                station_id, submitted_by=actor["user"],
                idempotency_key=idem)
            return self._send_json({"event": ev.to_dict()}, 201)
        if path == "/admin/freeze-rules":
            actor = self._require_role({"总决赛组委会"})
            ev = self.app.federation.freeze_federation_rules(
                expected_stations=body["expected_stations"],
                zone_priority=body["zone_priority"],
                submitted_by=actor["user"], idempotency_key=idem)
            return self._send_json({"event": ev.to_dict()}, 201)
        if path.startswith("/federation/project/"):
            station_id = path.split("/")[-1]
            self._require_station_scope(station_id)
            actor = self._actor()
            result = self.app.federation.project_station(
                station_id, submitted_by=actor["user"],
                idempotency_key=idem)
            return self._send_json({"projected": result}, 201)
        if path == "/federation/duplicates/resolve":
            actor = self._require_role({"总决赛组委会", "资格审核员"})
            events = self.app.federation.resolve_duplicates(
                submitted_by=actor["user"])
            return self._send_json(
                {"events": [e.to_dict() for e in events]}, 201)
        if path == "/federation/revoke":
            actor = self._require_role({"总决赛组委会"})
            ev = self.app.federation.revoke_qualification(
                body["station_id"], body["group"], body["person_id"],
                reason=body["reason"], submitted_by=actor["user"],
                idempotency_key=idem)
            return self._send_json({"event": ev.to_dict()}, 201)
        if path == "/federation/lock":
            actor = self._require_role({"总决赛组委会"})
            ev = self.app.federation.lock_final_roster(
                submitted_by=actor["user"], idempotency_key=idem)
            return self._send_json({"event": ev.to_dict()}, 201)
        raise ApiError(404, "未知路由")

    def _route_game(self, path, body, *, actor, idem):
        parts = path.split("/")
        station_id, game_id = parts[2], parts[4]
        suffix = "/".join(parts[5:])
        actor = self._require_station_scope(station_id)
        if actor["role"] != "裁判" and suffix != "":
            # 裁判提交成绩；赛区管理员可做纠错/申诉等管理动作
            pass
        common = dict(result_type=body.get("result_type", "胜负"),
                      outcome=body.get("outcome"),
                      record_hash=body.get("record_hash"),
                      record_kind=body.get("record_kind"),
                      signature=body.get("signature"),
                      submitted_by=actor["user"], idempotency_key=idem)
        svc = self.app.stations
        if suffix == "result":
            if actor["role"] != "裁判":
                raise ApiError(403, "只有裁判可以提交临场成绩")
            ev = svc.submit_result(station_id, game_id, note=body.get("note"),
                                   **common)
        elif suffix == "late-result":
            if actor["role"] != "裁判":
                raise ApiError(403, "迟到结果由裁判补录")
            ev = svc.record_late_result(
                station_id, game_id, reason=body["reason"],
                accepted_by=body["accepted_by"], **common)
        elif suffix == "correction":
            ev = svc.correct_result(
                station_id, game_id, reason=body["reason"], **common)
        elif suffix == "appeal":
            ev = svc.rule_appeal(
                station_id, game_id, appeal_id=body["appeal_id"],
                action=body["action"], basis=body["basis"],
                effective_outcome=body.get("effective_outcome"),
                result_type=body.get("result_type"),
                evidence_hashes=body.get("evidence_hashes", ()),
                ruled_by=body["ruled_by"],
                submitted_by=actor["user"], idempotency_key=idem)
        else:
            raise ApiError(404, "未知对局操作")
        effective = svc.effective_result(station_id, game_id)
        return self._send_json({"event": ev.to_dict(),
                                "effective": effective}, 201)
