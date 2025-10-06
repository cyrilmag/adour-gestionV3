from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.settings import get_settings


def build_templates() -> Jinja2Templates:
    settings = get_settings()
    return Jinja2Templates(directory=str(settings.templates_dir))


templates = build_templates()
