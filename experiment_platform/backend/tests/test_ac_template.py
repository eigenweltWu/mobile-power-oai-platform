from contextlib import contextmanager
from types import SimpleNamespace

from experiment_platform.backend.task_flow import AcTemplateRunner


class _Phone:
    def __init__(self):
        self.plans = []
        self.rearms = 0

    def session(self, plan):
        self.plans.append(plan)
        return {"ok": True}

    def rearm(self):
        self.rearms += 1
        return {"ok": True}


class _Db:
    def __init__(self):
        self.transitions = []

    def query_one(self, _sql, _params=()):
        return {"config_json": '{"ulTrafficMbps":12}'}

    def get_run(self, run_id):
        return {"run_id": run_id}

    def transition(self, run_id, state, note):
        self.transitions.append((run_id, state, note))


class _Oai:
    def __init__(self):
        self.started = []
        self.stops = 0

    def nettest_start(self, direction, protocol, target):
        self.started.append((direction, protocol, target))
        return {"session": {"state": "RUNNING", "running": True}}

    def nettest_stop(self):
        self.stops += 1
        return {"ok": True}


def test_ac_template_runs_platform_phases_and_auto_completes():
    phone, db, oai = _Phone(), _Db(), _Oai()

    class Flow:
        downlinks = {"ac-1": SimpleNamespace(sync_confirmed=True)}

        def __init__(self):
            self.db, self.oai, self.stopped = db, oai, False

        @contextmanager
        def _phone(self, _serial, _port):
            yield phone, {}

        def stop_experiment(self, *_args, **_kwargs):
            self.stopped = True
            return {"discarded": False}

    flow = Flow()
    runner = AcTemplateRunner(
        flow, "ac-1", "run-1", "phone", 8420,
        [{"name": "idle", "durationSeconds": 0.001},
         {"name": "loaded", "durationSeconds": 0.001,
          "configurationId": 7, "ulTrafficMbps": 12}], 7)

    runner.run()

    assert [plan["phases"][0]["name"] for plan in phone.plans] == ["idle", "loaded"]
    assert phone.rearms == 2
    assert oai.started == [("uplink", "udp", 12.0)]
    assert oai.stops >= 1
    assert flow.stopped is True
    assert db.transitions[-1][1] == "COMPLETE"
    assert runner.state == "completed"
