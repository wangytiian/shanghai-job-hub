"""Runtime configuration with explicit local, test, and cloud boundaries."""

from dataclasses import dataclass
import os


class SettingsError(ValueError):
    """Raised when the runtime environment is unsafe or incomplete."""


_VALID_ENVIRONMENTS = {"local", "test", "cloud"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    public_base_url: str = ""
    allowed_hosts: tuple[str, ...] = ()
    secret_backend: str = "windows"
    secret_revision: str = "local"
    app_version: str = "dev"
    bailian_secret_file: str = ""
    openai_compatible_secret_file: str = ""
    task_lease_seconds: int = 120
    task_heartbeat_seconds: int = 30
    task_queue_limit: int = 50

    def __post_init__(self) -> None:
        if self.app_env not in _VALID_ENVIRONMENTS:
            raise SettingsError("APP_ENV must be local, test, or cloud")
        if not self.database_url:
            raise SettingsError("DATABASE_URL is required")
        if self.app_env == "cloud" and self.database_url.startswith("sqlite"):
            raise SettingsError("cloud requires a PostgreSQL DATABASE_URL")
        if self.app_env == "cloud" and not self.database_url.startswith(("postgresql://", "postgresql+")):
            raise SettingsError("cloud requires a PostgreSQL DATABASE_URL")
        if self.task_lease_seconds <= 0 or self.task_heartbeat_seconds <= 0:
            raise SettingsError("task lease and heartbeat must be positive")
        if self.task_queue_limit <= 0:
            raise SettingsError("task queue limit must be positive")

    @property
    def is_cloud(self) -> bool:
        return self.app_env == "cloud"


def _csv_setting(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


def load_settings() -> Settings:
    app_env = os.getenv("APP_ENV", "local").strip().lower()
    database_url = os.getenv("DATABASE_URL", "sqlite:///data/recruiting_local.db").strip()
    return Settings(
        app_env=app_env,
        database_url=database_url,
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip(),
        allowed_hosts=_csv_setting("ALLOWED_HOSTS"),
        secret_backend=os.getenv("SECRET_BACKEND", "file" if app_env == "cloud" else "windows").strip(),
        secret_revision=os.getenv("SECRET_REVISION", "local").strip(),
        app_version=os.getenv("APP_VERSION", "dev").strip(),
        bailian_secret_file=os.getenv("BAILIAN_SECRET_FILE", "").strip(),
        openai_compatible_secret_file=os.getenv("OPENAI_COMPATIBLE_SECRET_FILE", "").strip(),
        task_lease_seconds=int(os.getenv("TASK_LEASE_SECONDS", "120")),
        task_heartbeat_seconds=int(os.getenv("TASK_HEARTBEAT_SECONDS", "30")),
        task_queue_limit=int(os.getenv("TASK_QUEUE_LIMIT", "50")),
    )
