from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    """Project level configuration resolved once at startup."""

    root_dir: Path
    database_path: Path
    xml_dir: Path
    static_dir: Path
    templates_dir: Path
    excel_dir: Path
    environment: str = "development"
    enable_xml_watcher: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        repo_root = Path(os.getenv("ADOUR_ROOT_DIR", Path(__file__).resolve().parent.parent))

        db_path = Path(os.getenv("ADOUR_DB_PATH", repo_root / "data" / "adour.db"))
        xml_dir = Path(os.getenv("ADOUR_XML_DIR", repo_root / "XML"))
        static_dir = Path(os.getenv("ADOUR_STATIC_DIR", repo_root / "static"))
        templates_dir = Path(os.getenv("ADOUR_TEMPLATES_DIR", repo_root / "templates"))
        excel_dir = Path(os.getenv("ADOUR_EXCEL_DIR", repo_root / "excel"))
        environment = os.getenv("ADOUR_ENV", "development")

        watcher_flag = os.getenv("ADOUR_XML_WATCHER", "0").strip().lower()
        enable_xml_watcher = watcher_flag in {"1", "true", "on", "yes"}

        return cls(
            root_dir=repo_root,
            database_path=db_path,
            xml_dir=xml_dir,
            static_dir=static_dir,
            templates_dir=templates_dir,
            excel_dir=excel_dir,
            environment=environment,
            enable_xml_watcher=enable_xml_watcher,
        )

    def sqlite_path(self) -> str:
        """Return the string path used by sqlite3.connect."""
        return str(self.database_path.resolve())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings.from_env()
