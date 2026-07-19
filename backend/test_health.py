"""Health-endpoint tests.

/health must report health in the HTTP STATUS CODE, not only the body — an
uptime monitor checks the status. 200 when the database answers, 503 when it
doesn't. (Before this, a dead database returned 200 with "down" in the body,
so no monitor could see it.)

Run:  python backend/test_health.py     (exit 0 = pass)
"""
import main
from fastapi.responses import JSONResponse


def _health_endpoint():
    return next(r for r in main.app.routes if getattr(r, "path", "") == "/health").endpoint


def test_health_ok_when_db_answers():
    resp = _health_endpoint()()
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 200
    import json
    body = json.loads(resp.body)
    assert body["database"] == "ok" and body["status"] == "ok"
    print("PASS /health returns 200 when the DB answers")


def test_health_503_when_db_down():
    # Force the DB probe to fail, exactly like an unreachable database.
    import platform_routes
    original = platform_routes.engine
    class _DeadEngine:
        def connect(self):
            raise RuntimeError("could not connect to server")
    platform_routes.engine = _DeadEngine()
    try:
        resp = _health_endpoint()()
    finally:
        platform_routes.engine = original
    assert resp.status_code == 503, "a dead DB must surface as 503, not 200"
    import json
    body = json.loads(resp.body)
    assert body["database"] == "down" and body["status"] == "degraded"
    print("PASS /health returns 503 when the DB is down")


if __name__ == "__main__":
    test_health_ok_when_db_answers()
    test_health_503_when_db_down()
    print("ALL HEALTH TESTS PASSED")
