"""ASGI entry point for the Ghost platform (unified control plane + observability).

直接复用 `ghost.app` 的模块级 app 实例,不再二次 `create_app()`:
模块导入本身已构造过一次,重复构造会在同进程内建出第二个控制面 Store
(第二个 SQLite 连接 + 第二遍 `_recover_inflight_containers`),是纯粹的副作用。
"""

from ghost.app import app


if __name__ == "__main__":
    import uvicorn

    from ghost.control.config import Settings

    settings = Settings.from_env()
    uvicorn.run(app, host=settings.host, port=settings.port)
