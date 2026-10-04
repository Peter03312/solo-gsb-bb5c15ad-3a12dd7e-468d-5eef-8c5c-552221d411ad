"""API end-to-end tests using an ephemeral SQLite database."""
import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="planner-test-"), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

DIRECT_REQUEST = {
    "request_id": "api-direct-001",
    "stock": {"concentration": "1", "volume_nl": 100000},
    "diluent": {"volume_nl": 1000000},
    "pipette": {"min_volume_nl": 1000, "max_volume_nl": 10000, "step_nl": 100},
    "well_capacity_nl": 50000,
    "targets": [
        {"well_id": "A1", "volume_nl": 20000, "concentration": "1/2"},
        {"well_id": "A2", "volume_nl": 16000, "concentration": "0.25"},
    ],
}


def test_create_and_replay_plan():
    with TestClient(app) as client:
        r1 = client.post("/v1/plans", json=DIRECT_REQUEST)
        assert r1.status_code == 200, r1.text
        body = r1.json()
        assert body["status"] == "solved"
        assert body["transfers"], "expected a non-empty transfer list"
        wells = {w["well_id"]: w for w in body["wells"]}
        assert wells["A1"]["final_volume_nl"] == 20000
        assert wells["A1"]["final_concentration"] == "1/2"
        assert wells["A2"]["final_volume_nl"] == 16000
        assert wells["A2"]["final_concentration"] == "1/4"
        steps = [t["step"] for t in body["transfers"]]
        assert steps == list(range(1, len(steps) + 1))

        # Idempotent replay: same request_id + same content -> frozen plan.
        r2 = client.post("/v1/plans", json=DIRECT_REQUEST)
        assert r2.status_code == 200
        assert r2.json() == body

        # Equivalent content in a different representation replays too.
        equivalent = dict(DIRECT_REQUEST, targets=[
            {"well_id": "A2", "volume_nl": 16000, "concentration": "1/4"},
            {"well_id": "A1", "volume_nl": 20000, "concentration": "0.5"},
        ])
        r3 = client.post("/v1/plans", json=equivalent)
        assert r3.status_code == 200
        assert r3.json() == body

        # GET returns the same frozen plan.
        r4 = client.get("/v1/plans/api-direct-001")
        assert r4.status_code == 200
        assert r4.json() == body


def test_request_id_conflict():
    with TestClient(app) as client:
        changed = dict(DIRECT_REQUEST)
        changed["diluent"] = {"volume_nl": 999999}
        r = client.post("/v1/plans", json=changed)
        assert r.status_code == 409
        assert r.json()["error"] == "request_id_conflict"


def test_infeasible_is_reported_without_partial_plan():
    req = {
        "request_id": "api-infeasible-001",
        "stock": {"concentration": "1/10", "volume_nl": 100000},
        "diluent": {"volume_nl": 1000000},
        "pipette": {"min_volume_nl": 1000, "max_volume_nl": 10000,
                    "step_nl": 100},
        "well_capacity_nl": 50000,
        "targets": [{"well_id": "A1", "volume_nl": 20000,
                     "concentration": "1/5"}],
    }
    with TestClient(app) as client:
        r = client.post("/v1/plans", json=req)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "infeasible"
        assert "transfers" not in body  # never a partial plan
        assert body["blocked_targets"][0]["well_id"] == "A1"
        assert body["blocked_targets"][0]["reasons"]

        # Infeasible results are frozen as well.
        r2 = client.post("/v1/plans", json=req)
        assert r2.status_code == 200
        assert r2.json() == body


def test_invalid_values_rejected():
    bad_volume = dict(DIRECT_REQUEST, request_id="api-bad-001",
                      diluent={"volume_nl": -5})
    unknown_via = dict(DIRECT_REQUEST, request_id="api-bad-002", targets=[
        {"well_id": "A1", "volume_nl": 20000, "concentration": "1/2",
         "via": "NOPE"},
    ])
    bad_conc = dict(DIRECT_REQUEST, request_id="api-bad-003",
                    stock={"concentration": "-1/2", "volume_nl": 1000})
    bad_pipette = dict(DIRECT_REQUEST, request_id="api-bad-004",
                       pipette={"min_volume_nl": 9000, "max_volume_nl": 1000,
                                "step_nl": 100})
    with TestClient(app) as client:
        for req in (bad_volume, unknown_via, bad_conc, bad_pipette):
            r = client.post("/v1/plans", json=req)
            assert r.status_code == 400, (req["request_id"], r.status_code,
                                          r.text)
            assert r.json()["error"] == "invalid_request"


def test_get_unknown_plan_404():
    with TestClient(app) as client:
        r = client.get("/v1/plans/does-not-exist")
        assert r.status_code == 404


def test_healthz():
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
