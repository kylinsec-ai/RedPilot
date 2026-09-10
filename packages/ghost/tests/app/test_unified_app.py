"""统一 app 装配回归。

覆盖三类曾经真实发生、且在测试里静默通过的装配缺陷:
  1. canonical outbox 从未启动 —— 控制面 app 的路由被复制进主 app 而非 mount,
     Starlette 不会执行未挂载子应用的 lifespan,投递任务因此从未创建;
  2. `app.state.store` 命名冲突 —— 被赋为控制面 Store,而观测读端读同一个名字,
     导致 /api/status、/api/roster、/api/runs 全部 500、SPA 404;
  3. web_dir/control_url 被无必要地加 `obs_` 前缀,读端读不到。

注:合并期间的 obs 测试构造的是 `ghost.obs.app.create_app`(独立工厂),它设置的是
**旧**名字,所以上述缺陷在测试里全绿却线上全废 —— 本文件专门针对统一 app 装配断言。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from ghost.app import create_app
from ghost.control.config import Settings as ControlSettings
from ghost.control.store import Store as ControlStore
from ghost.obs.config import Settings as ObsSettings
from ghost.obs.store import ObsStore

TASK_TOKEN = "task-unified"
ADMIN_TOKEN = "admin-unified"
WORKER_TOKEN = "worker-unified"
OBS_TOKEN = "obs-unified"

# 观测读端凭据(未单独配置 OBSERVABILITY_READ_TOKEN 时回落 ingest token)
READ = {"X-Observability-Token": OBS_TOKEN}

TASKS = {
    "token": TASK_TOKEN,
    "challenges": [
        {
            "unique_code": "web-01",
            "description": "unified app test",
            "flags": ["flag{unified}"],
            "container_addr": ["10.0.0.1:80"],
        }
    ],
}


@pytest.fixture()
def web_dir(tmp_path):
    """最小 SPA 产物目录(只有 index.html 即够 / 路由断言)。"""
    d = tmp_path / "web"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>ghost</title>", encoding="utf-8")
    return d


def _app(tmp_path, web_dir, *, observability_url=None, observability_token=None):
    return create_app(
        control_settings=ControlSettings(
            database_path=str(tmp_path / "control.sqlite3"),
            benchmark_token=TASK_TOKEN,
            admin_token=ADMIN_TOKEN,
            worker_token=WORKER_TOKEN,
            observability_url=observability_url,
            observability_token=observability_token,
        ),
        obs_settings=ObsSettings(
            obs_token=OBS_TOKEN,
            db_path=str(tmp_path / "obs.sqlite3"),
            web_dir=str(web_dir),
        ),
        tasks=TASKS,
    )


def test_outbox_dispatch_started(tmp_path, web_dir, monkeypatch):
    """lifespan 必须真的把 canonical 投递任务装配起来(曾经完全没有)。"""
    started: list[tuple] = []
    import asyncio

    import ghost.control.outbox as obx

    def _spy(store, url, token):
        started.append((store, url, token))
        # 真 task:lifespan 退出时 stop_dispatch_task 会 cancel/await 它
        return asyncio.create_task(asyncio.sleep(3600))

    monkeypatch.setattr(obx, "start_dispatch_task", _spy)
    app = _app(
        tmp_path, web_dir,
        observability_url="http://obs.invalid",
        observability_token=OBS_TOKEN,
    )
    with TestClient(app):
        pass
    assert started, "outbox 投递任务未装配 —— 权威事件通道断开"
    assert started[0][1] == "http://obs.invalid"
    assert started[0][2] == OBS_TOKEN


def test_outbox_not_started_when_unconfigured(tmp_path, web_dir, monkeypatch):
    """未配置观测地址时不装配(且不得因此报错)。"""
    started: list[tuple] = []
    import ghost.control.outbox as obx

    monkeypatch.setattr(obx, "start_dispatch_task",
                        lambda *a, **k: started.append(a))
    with TestClient(_app(tmp_path, web_dir)):
        pass
    assert started == []


def test_state_names_do_not_collide(tmp_path, web_dir):
    """app.state.store 恒为观测 store;控制面 store 在 control_store。"""
    app = _app(tmp_path, web_dir)
    with TestClient(app):
        assert isinstance(app.state.store, ObsStore)
        assert isinstance(app.state.control_store, ControlStore)
        assert app.state.store is app.state.obs_store
        # 读端依赖的其余 state 键
        assert app.state.web_dir == str(web_dir)
        assert app.state.obs_token == OBS_TOKEN
        assert app.state.bus is not None


def test_obs_read_endpoints_serve(tmp_path, web_dir):
    """曾经全部 500('Store' object has no attribute ...)的读端。"""
    with TestClient(_app(tmp_path, web_dir), headers=READ) as client:
        for path in ("/api/health", "/api/status", "/api/roster", "/api/runs"):
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} -> {resp.status_code}"


def test_read_requires_credentials(tmp_path, web_dir):
    """观测读端返回明文 flag 与完整实录,必须凭据(worker 持 ingest token 且同网)。"""
    with TestClient(_app(tmp_path, web_dir)) as anon:  # 无默认 header
        for path in ("/api/status", "/api/roster", "/api/runs", "/api/challenge?code=web-01"):
            assert anon.get(path).status_code == 401, path
        assert anon.get("/api/events").status_code == 401
        assert anon.get("/api/runs", headers={"X-Observability-Token": "wrong"}).status_code == 401
    # 公开面:SPA 外壳与存活探测(不含数据,否则登录界面本身无法渲染)
    with TestClient(_app(tmp_path, web_dir)) as anon:
        assert anon.get("/").status_code == 200
        assert anon.get("/api/health").status_code == 200
    # 带凭据 → 放行
    with TestClient(_app(tmp_path, web_dir), headers=READ) as ok:
        assert ok.get("/api/runs").status_code == 200


def test_spa_index_served(tmp_path, web_dir):
    """曾经 404('web dir not configured'):根路径必须给出 SPA。"""
    with TestClient(_app(tmp_path, web_dir)) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "<!doctype html" in resp.text.lower()


def test_control_routes_still_served(tmp_path, web_dir):
    """控制面路由并入主 app 后仍可用(且不经 mount)。"""
    with TestClient(_app(tmp_path, web_dir)) as client:
        # 参与方凭据走 BENCHMARK_TOKEN 头(注意不是 Authorization: Bearer)
        resp = client.get("/openapi/v1/challenges", headers={"BENCHMARK_TOKEN": TASK_TOKEN})
        assert resp.status_code == 200
        assert resp.json()[0]["unique_code"] == "web-01"
        # 未带凭据 → 不得放行(404 而非 401:不给出 token 是否存在的信号)
        assert client.get("/openapi/v1/challenges").status_code == 404


def test_default_routes_not_duplicated(tmp_path, web_dir):
    """控制面的默认路由(openapi/docs/redoc)不得并入,否则与主 app 同名重复。"""
    app = _app(tmp_path, web_dir)
    paths = [getattr(r, "path", None) for r in app.router.routes]
    assert paths.count("/openapi.json") == 1
    assert paths.count("/docs") == 1
