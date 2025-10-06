from __future__ import annotations

from app.main import create_app

app = create_app()


if __name__ == "__main__":  # pragma: no cover - dev helper
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
