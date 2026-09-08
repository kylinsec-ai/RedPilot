"""ASGI entry point for the TSecBench platform."""

from tsecbench.api import create_app

app = create_app()


if __name__ == "__main__":
    import uvicorn

    from tsecbench.config import Settings

    settings = Settings.from_env()
    uvicorn.run(app, host=settings.host, port=settings.port)
