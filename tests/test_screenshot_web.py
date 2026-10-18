from io import BytesIO
from PIL import Image
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import ScreenshotImport


def screenshot_app(tmp_path):
    return create_app(settings=Settings(app_env="test", database_url="sqlite+pysqlite:///:memory:",
                      secret_backend="file", screenshot_upload_root=str(tmp_path / "uploads"), screenshot_executor_enabled=False))


def test_screenshot_upload_creates_recoverable_work_area_without_ai(tmp_path):
    app = screenshot_app(tmp_path)
    client = TestClient(app)
    page = client.get("/imports/screenshots")
    assert page.status_code == 200
    assert "截图转招聘内容" in page.text
    image = BytesIO()
    Image.new("RGB", (400, 600), "white").save(image, "PNG")
    response = client.post("/imports/screenshots", data={"submission_key":"web-test-one", "source_name":"招聘公众号"},
                           files={"images":("article.png",image.getvalue(),"image/png")}, follow_redirects=False)
    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "微信群" in detail.text and "内部草稿" in detail.text
    assert 'data-copy-key="group_message"' not in detail.text
    with app.state.session_factory() as session:
        batch = session.query(ScreenshotImport).one()
        assert batch.job_id is None
        assert batch.status in {"uploaded", "needs_attention"}
        assert batch.source_name == "招聘公众号"


def test_oversized_request_rejected_even_without_csrf_header(tmp_path):
    client = TestClient(screenshot_app(tmp_path))
    response = client.post("/imports/screenshots", content=b"body", headers={"content-length":str(27*1024*1024)})
    assert response.status_code == 413
