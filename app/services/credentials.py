"""Credential stores selected by the runtime, never by browser input in cloud."""

from pathlib import Path


class FileCredentialStore:
    """Read a single provider key from a mounted, read-only file."""

    def __init__(self, path: Path | None):
        self.path = path

    def get_secret(self) -> str | None:
        if self.path is None:
            return None
        try:
            value = self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def has_secret(self) -> bool:
        return bool(self.get_secret())

    def set_secret(self, value: str) -> None:
        raise PermissionError("云端模型密钥只能通过受控 Secret 文件轮换")


class SecretProvider:
    def __init__(self, provider_paths: dict[str, Path | None]):
        self.provider_paths = provider_paths

    def for_provider(self, provider: str) -> FileCredentialStore:
        try:
            return FileCredentialStore(self.provider_paths[provider])
        except KeyError as exc:
            raise ValueError("未知模型提供方") from exc
