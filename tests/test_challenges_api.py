from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tsecbench.api import create_app
from tsecbench.config import Settings


TOKEN = "task-token"
HEADERS = {"BENCHMARK_TOKEN": TOKEN}


def task_config(*, expires_at: str | None = None, addresses: list[str] | None = None) -> dict:
    return {
        "token": TOKEN,
        "expires_at": expires_at,
        "challenges": [
            {
                "unique_code": "web-01",
                "description": "A web challenge",
                "difficulty": "easy",
                "level": 1,
                "total_score": 100,
                "flags": [
                    {"value": "flag{one}", "score": 40},
                    {"value": "flag{two}", "score": 60},
                ],
                "container_addr": ["10.0.0.5:8080"] if addresses is None else addresses,
                "hint": "Check the login form",
                "hint_cost_radio": 0.25,
            }
        ],
    }


def make_client(tmp_path, **kwargs) -> TestClient:
    app = create_app(database_path=str(tmp_path / "bench.sqlite3"), tasks=task_config(**kwargs))
    return TestClient(app)


def assert_business_error(response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["detail"] == {}


def test_authentication_and_list_shape(tmp_path):
    client = make_client(tmp_path)

    assert_business_error(client.get("/openapi/v1/challenges"), 404, "task_not_found")
    response = client.get("/openapi/v1/challenges", headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == [
        {
            "unique_code": "web-01",
            "description": "A web challenge",
            "difficulty": "easy",
            "level": 1,
            "total_score": 100,
            "flag_count": 2,
            "correct_flag_count": 0,
            "is_completed": False,
            "container_status": "stopped",
            "container_addr": [],
        }
    ]


def test_lifecycle_hint_scoring_duplicate_and_close(tmp_path):
    client = make_client(tmp_path)

    start = client.post("/openapi/v1/challenges/start?unique_code=web-01", headers=HEADERS)
    assert start.status_code == 200
    assert start.json() == {"unique_code": "web-01", "container_addr": ["10.0.0.5:8080"]}

    hint = client.get("/openapi/v1/challenges/hint?unique_code=web-01", headers=HEADERS)
    assert hint.json() == {"unique_code": "web-01", "hint": "Check the login form"}

    first = client.post(
        "/openapi/v1/challenges/submit",
        headers=HEADERS,
        json={"unique_code": "web-01", "flag": "flag{one}"},
    )
    assert first.status_code == 200
    assert first.json() == {
        "correct": True,
        "awarded": 30,
        "cumulative_score": 30,
        "correct_flag_count": 1,
        "total_flag_count": 2,
        "matched_flag_index": 0,
    }

    wrong = client.post(
        "/openapi/v1/challenges/submit",
        headers=HEADERS,
        json={"unique_code": "web-01", "flag": "not-a-flag"},
    )
    assert wrong.status_code == 200
    assert wrong.json()["correct"] is False
    assert wrong.json()["cumulative_score"] == 30
    assert wrong.json()["matched_flag_index"] is None

    duplicate = client.post(
        "/openapi/v1/challenges/submit",
        headers=HEADERS,
        json={"unique_code": "web-01", "flag": "flag{one}"},
    )
    assert_business_error(duplicate, 409, "duplicate")

    second = client.post(
        "/openapi/v1/challenges/submit",
        headers=HEADERS,
        json={"unique_code": "web-01", "flag": "flag{two}"},
    )
    assert second.json()["correct_flag_count"] == 2
    assert second.json()["cumulative_score"] == 75

    completed = client.get("/openapi/v1/challenges/hint?unique_code=web-01", headers=HEADERS)
    assert_business_error(completed, 409, "invalid_state")

    close = client.post("/openapi/v1/challenges/close?unique_code=web-01", headers=HEADERS)
    assert close.json() == {"unique_code": "web-01", "closed": True}
    assert client.get("/openapi/v1/challenges", headers=HEADERS).json()[0]["container_addr"] == []


def test_validation_and_unknown_challenge_errors(tmp_path):
    client = make_client(tmp_path)

    too_long = client.post(
        "/openapi/v1/challenges/submit",
        headers=HEADERS,
        json={"unique_code": "web-01", "flag": "x" * 4097},
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"][0]["loc"] == ["body", "flag"]

    missing_code = client.post("/openapi/v1/challenges/start", headers=HEADERS)
    assert missing_code.status_code == 422

    for path in (
        "/openapi/v1/challenges/start?unique_code=missing",
        "/openapi/v1/challenges/hint?unique_code=missing",
        "/openapi/v1/challenges/close?unique_code=missing",
    ):
        assert_business_error(client.request("POST" if "/start" in path or "/close" in path else "GET", path, headers=HEADERS), 404, "challenge_not_found")

    submit = client.post(
        "/openapi/v1/challenges/submit",
        headers=HEADERS,
        json={"unique_code": "missing", "flag": "x"},
    )
    assert_business_error(submit, 404, "challenge_not_found")


def test_active_limit_and_resource_state(tmp_path):
    config = task_config()
    config["challenges"] = [
        {
            **config["challenges"][0],
            "unique_code": f"web-{index}",
            "container_addr": [f"10.0.0.{index}:8080"],
        }
        for index in range(4)
    ]
    app = create_app(database_path=str(tmp_path / "limit.sqlite3"), tasks=config, max_active_challenges=3)
    client = TestClient(app)
    for index in range(3):
        assert client.post(f"/openapi/v1/challenges/start?unique_code=web-{index}", headers=HEADERS).status_code == 200
    assert_business_error(client.post("/openapi/v1/challenges/start?unique_code=web-3", headers=HEADERS), 409, "invalid_state")

    unavailable = make_client(tmp_path / "unavailable", addresses=[])
    assert_business_error(unavailable.post("/openapi/v1/challenges/start?unique_code=web-01", headers=HEADERS), 503, "resource_unavailable")


def test_expired_task_is_rejected_and_state_persists(tmp_path):
    expiry = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    client = make_client(tmp_path, expires_at=expiry)
    assert_business_error(client.get("/openapi/v1/challenges", headers=HEADERS), 409, "invalid_state")

    persistent_path = tmp_path / "persistent.sqlite3"
    app = create_app(database_path=str(persistent_path), tasks=task_config())
    client = TestClient(app)
    assert client.post("/openapi/v1/challenges/start?unique_code=web-01", headers=HEADERS).status_code == 200
    assert client.post(
        "/openapi/v1/challenges/submit",
        headers=HEADERS,
        json={"unique_code": "web-01", "flag": "flag{one}"},
    ).json()["correct"] is True

    restarted = create_app(database_path=str(persistent_path), tasks=task_config())
    restarted_client = TestClient(restarted)
    challenge = restarted_client.get("/openapi/v1/challenges", headers=HEADERS).json()[0]
    assert challenge["correct_flag_count"] == 1
    assert challenge["container_status"] == "available"

def test_settings_loads_benchmark_values_from_dotenv(tmp_path, monkeypatch):
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "BENCHMARK_BASE_URL=https://benchmark.example.test\n"
        "BENCHMARK_TOKEN=file-token\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BENCHMARK_BASE_URL", raising=False)
    monkeypatch.delenv("BENCHMARK_TOKEN", raising=False)

    settings = Settings.from_env(env_file=dotenv_file)

    assert settings.benchmark_base_url == "https://benchmark.example.test"
    assert settings.benchmark_token == "file-token"


def test_settings_environment_token_overrides_dotenv(tmp_path, monkeypatch):
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "BENCHMARK_BASE_URL=https://benchmark.example.test\n"
        "BENCHMARK_TOKEN=file-token\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BENCHMARK_BASE_URL", raising=False)
    monkeypatch.setenv("BENCHMARK_TOKEN", "exported-token")

    settings = Settings.from_env(env_file=dotenv_file)

    assert settings.benchmark_base_url == "https://benchmark.example.test"
    assert settings.benchmark_token == "exported-token"


def test_frontend_serves_console_and_static_assets(tmp_path):
    client = make_client(tmp_path)

    index = client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert "TSecBench" in index.text

    styles = client.get("/static/styles.css")
    assert styles.status_code == 200
    assert "text/css" in styles.headers["content-type"]

    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
