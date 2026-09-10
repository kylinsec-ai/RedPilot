"""ASGI entry point for the Ghost platform."""

from ghost.app import create_app

app = create_app()


if __name__ == "__main__":
    import uvicorn

    from ghost.control.config import Settings

    settings = Settings.from_env()
    uvicorn.run(app, host=settings.host, port=settings.port)
