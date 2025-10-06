from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.migrations.schema import apply_database_defaults
from app.services.etl import run_folder
from app.services.xml_watcher import start_xml_watcher, XmlWatcher
from app.settings import get_settings

from modules.routes.dashboard import router as dashboard_router
from modules.routes.tables import router as tables_router
from modules.routes.parameters import router as parameters_router
from modules.routes.imports import router as imports_router
from modules.routes.interventions_xml import router as xml_router
from modules.routes.garanties import router as garanties_router
from modules.routes.maintenance import (
    router as maintenance_router,
    public_router as maintenance_public_router,
)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Adour Gestion")

    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

    app.include_router(maintenance_router)
    app.include_router(maintenance_public_router)
    app.include_router(dashboard_router)
    app.include_router(tables_router)
    app.include_router(parameters_router)
    app.include_router(imports_router)
    app.include_router(xml_router)
    app.include_router(garanties_router)

    xml_watcher_handle: XmlWatcher | None = None

    @app.on_event("startup")
    async def on_startup() -> None:  # pragma: no cover - startup side effects
        nonlocal xml_watcher_handle
        apply_database_defaults()
        try:
            run_folder(get_settings().sqlite_path(), get_settings().xml_dir)
        except Exception as exc:
            print(f"[ETL][startup] Erreur update XML: {exc}")

        if get_settings().enable_xml_watcher:
            xml_watcher_handle = start_xml_watcher()

    @app.on_event("shutdown")
    async def on_shutdown() -> None:  # pragma: no cover - shutdown side effects
        if xml_watcher_handle:
            xml_watcher_handle.stop()

    return app


app = create_app()
