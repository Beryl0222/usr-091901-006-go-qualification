"""应用装配：一个事件日志 + 四个领域服务，支持从 JSONL 完整重放。"""

from events import EventStore
from federation import FederationService
from identity import IdentityService
from station import StationService


class Application:
    def __init__(self, *, store_path=None, master_secret="go-qualification-secret",
                 clock=None):
        self.store = EventStore(store_path, clock=clock)
        self.identity = IdentityService(self.store, master_secret=master_secret)
        self.stations = StationService(self.store, self.identity)
        self.federation = FederationService(self.store, self.identity,
                                            self.stations)
        # 开赛后（任一轮配对已冻结或已终赛）禁止新增报名
        def registration_gate(station_id, group):
            if self.stations.finalized.get(station_id):
                from events import EventError
                raise EventError("该赛区已终赛，不能再报名")
            if self.stations.pairings.get(station_id):
                from events import EventError
                raise EventError("该赛区配对已冻结，报名通道已关闭")

        self.identity.station_gate = registration_gate

    def audit(self):
        return {"chain_ok": self.store.verify_chain(),
                "event_count": len(self.store),
                "tail_hash": self.store.tail_hash}


def rebuild(store_path, **kwargs):
    """从持久化事件日志重建整个应用（聚合订阅时自动重放）。"""
    return Application(store_path=store_path, **kwargs)
