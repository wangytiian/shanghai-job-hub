"""Shared AI service construction for the web app and background workers."""

from pathlib import Path

from app.config import Settings
from app.services.ai_settings import AiSettingsService, OpenAICompatibleClient, WindowsCredentialStore
from app.services.credentials import SecretProvider
from app.services.outbound_policy import validate_public_http_url


def create_ai_settings_service(settings: Settings) -> AiSettingsService:
    if settings.is_cloud or settings.secret_backend == "file":
        secret_provider = SecretProvider({
            "bailian": Path(settings.bailian_secret_file) if settings.bailian_secret_file else None,
            "openai_compatible": Path(settings.openai_compatible_secret_file) if settings.openai_compatible_secret_file else None,
        })
        return AiSettingsService(
            credential_store_factory=secret_provider.for_provider,
            openai_client=OpenAICompatibleClient(validate_outbound=validate_public_http_url),
            credential_revision=settings.secret_revision,
        )
    return AiSettingsService(WindowsCredentialStore())
