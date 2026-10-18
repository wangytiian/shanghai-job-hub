from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_compose_test_defines_isolated_postgres_and_runs_the_full_suite():
    content = (PROJECT_ROOT / "compose.test.yaml").read_text(encoding="utf-8")

    assert "name: recruitingops_test" in content
    assert "postgres:16-alpine" in content
    assert "POSTGRES_DB: recruiting_test" in content
    assert "DATABASE_URL: postgresql+psycopg://" in content
    assert 'command: ["pytest", "-q"]' in content


def test_test_image_includes_application_migrations_and_tests_without_changing_production_image():
    content = (PROJECT_ROOT / "Dockerfile.test").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in content
    assert "COPY app ./app" in content
    assert "COPY migrations ./migrations" in content
    assert "COPY tests ./tests" in content


def test_cloud_web_and_worker_share_private_screenshot_volume():
    content = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "SCREENSHOT_UPLOAD_ROOT: /var/lib/recruiting/uploads" in content
    assert 'SCREENSHOT_EXECUTOR_ENABLED: "0"' in content
    for service, next_service in (("web", "worker"), ("worker", "proxy")):
        section = content.split(f"  {service}:\n", 1)[1].split(f"  {next_service}:\n", 1)[0]
        assert "recruitment-uploads:/var/lib/recruiting/uploads" in section
    proxy = content.split("  proxy:\n", 1)[1].split("\nnetworks:", 1)[0]
    assert "recruitment-uploads" not in proxy
