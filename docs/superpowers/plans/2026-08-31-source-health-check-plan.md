# 来源官网连接自检 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 B/C/D 类来源提供安全、可解释、手动触发的官网连接自检。

**Architecture:** 新服务仅访问`Source.official_career_url or Source.url`，将结果写回来源监控字段。路由调用服务后重新渲染来源页并显示反馈；模板仅为可检查来源显示按钮，A 类自动采集入口不改变。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、httpx、Jinja2、pytest。

## Global Constraints

- 严格保留 HTTPS 证书校验、15 秒超时和最多 3 次重定向。
- 不接收任意 URL，不下载附件，不抓取岗位，不创建招聘记录。
- 连接失败不自动暂停来源、不改变分层或自动采集资格。
- 对用户只显示简洁错误类型，不返回内部堆栈。

---

### Task 1: 安全连接检查服务

**Files:**
- Create: `app/services/source_health.py`
- Test: `tests/test_source_health.py`

**Interfaces:**
- Consumes: `Source`、可注入的 httpx 风格客户端、`checked_at: datetime`。
- Produces: `check_source_connection(source, client, checked_at) -> SourceHealthResult`，并更新来源的最近检查和摘要字段。

- [ ] **Step 1: Write failing tests**

```python
def test_connection_check_records_success_without_changing_collection_rights(session):
    source = Source(name="测试来源", url="https://official.example/jobs", official_career_url="https://official.example/jobs", library_tier="B", is_enabled=False)
    session.add(source); session.commit()
    result = check_source_connection(source, FakeClient(status_code=200), datetime(2026, 8, 31, 12, 0))
    assert result.kind == "success"
    assert "官网连接正常" in source.last_monitor_summary
    assert source.library_tier == "B" and source.is_enabled is False

def test_connection_check_converts_tls_failure_to_safe_message(session):
    result = check_source_connection(source, FailingClient(httpx.ConnectError("certificate verify failed")), datetime(2026, 8, 31, 12, 0))
    assert result.kind == "error"
    assert source.last_error_summary == "HTTPS 证书或连接校验失败"
```

- [ ] **Step 2: Run tests red**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_source_health.py -q`

Expected: FAIL because module is not defined.

- [ ] **Step 3: Implement minimal service**

```python
@dataclass(frozen=True)
class SourceHealthResult:
    kind: str
    message: str

def check_source_connection(source, client, checked_at):
    url = source.official_career_url or source.url
    try:
        response = client.get(url, follow_redirects=True, timeout=15.0, max_redirects=3)
        response.raise_for_status()
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        source.last_error_summary = "HTTPS 证书或连接校验失败"
        source.last_monitor_summary = "官网连接异常：HTTPS 证书或连接校验失败"
        source.last_checked_at = checked_at
        return SourceHealthResult("error", source.last_monitor_summary)
    source.last_checked_at = checked_at
    source.last_error_summary = ""
    source.last_monitor_summary = "官网连接正常，仍待专用适配，不参与每日采集"
    return SourceHealthResult("success", source.last_monitor_summary)
```

- [ ] **Step 4: Run focused tests**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_source_health.py -q`

Expected: PASS.

### Task 2: 来源页操作与反馈

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/sources.html`
- Modify: `tests/test_source_monitor.py`

**Interfaces:**
- Consumes: `POST /sources/{source_id}/health-check`。
- Produces: 重新渲染的`/sources`页面，其中`health_feedback`显示一次，来源行显示更新后的摘要。

- [ ] **Step 1: Write failing tests**

```python
def test_source_health_check_route_returns_feedback_on_sources_page(monkeypatch):
    app = create_app("sqlite+pysqlite:///:memory:")
    client = TestClient(app)
    source_id = get_b_tier_source_id(app)
    response = client.post(f"/sources/{source_id}/health-check")
    assert response.status_code == 200
    assert "官网连接正常" in response.text
    assert "验证官网连接" in response.text
```

- [ ] **Step 2: Run test red**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_source_monitor.py::test_source_health_check_route_returns_feedback_on_sources_page -q`

Expected: FAIL with HTTP 404.

- [ ] **Step 3: Implement route and template**

将来源页渲染提取为闭包`render_sources(request, session, health_feedback="", health_feedback_kind="")`；新增路由，只允许`library_tier != "A"`来源检查，调用`check_source_connection`，提交事务并返回同页。模板在按钮区新增：

```html
{% elif source.library_tier != 'A' %}
<form method="post" action="/sources/{{ source.id }}/health-check">
  <button class="button button-secondary" type="submit">验证官网连接</button>
</form>
{% endif %}
```

并在标题下按`health_feedback_kind`显示成功或异常提示。

- [ ] **Step 4: Run focused and full tests**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_source_monitor.py -q`，再运行`.\\.venv\\Scripts\\python.exe -m pytest -q`。

Expected: 全部通过。
