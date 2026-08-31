# 上海华智公考公众号线索源接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将上海华智公考的公开文章作为可追溯、不可自动发布的招聘线索导入系统。

**Architecture:** 在来源目录增加一个 C 类公众号线索源；新增独立服务只读取运营人员提供的公开文章 URL，并把合格文本转化为现有 `Job` 待核验记录。现有审核、结构化和分发边界保持不变。

**Tech Stack:** FastAPI、SQLAlchemy、httpx、Python 标准库 HTMLParser、pytest。

## Global Constraints

- 不绕过登录、验证码、反爬或微信公众号访问限制。
- 只接受公开 `mp.weixin.qq.com` 文章链接。
- 公众号仅为线索，不自动采集、不自动发布；官方原文与报名入口仍由人工补充。
- 当前目录不是 Git 仓库，不执行提交操作。

---

### Task 1: 目录与来源层级

**Files:**
- Modify: `app/sources/catalog.py`
- Test: `tests/test_source_catalog.py`

**Interfaces:**
- Produces: `Source(name="上海华智公考（公众号招聘线索）", adapter_key="wechat_article_lead")`。

- [ ] **Step 1: 写失败测试**

```python
def test_catalog_includes_huazhi_wechat_as_non_collecting_c_tier_source():
    source = next(item for item in OFFICIAL_SOURCE_CATALOG if item.name == "上海华智公考（公众号招聘线索）")
    assert source.library_tier == "C"
    assert source.is_enabled is False
    assert source.adapter_key == "wechat_article_lead"
```

- [ ] **Step 2: 运行测试，确认因来源不存在失败**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_source_catalog.py -q`

- [ ] **Step 3: 最小实现**

在 C 类来源开头加入 `_source("上海华智公考（公众号招聘线索）", "https://www.huazhi.cn/", "上海公职招考线索", "C", 72, adapter_key="wechat_article_lead", source_type="公众号线索源")`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_source_catalog.py -q`

### Task 2: 公共文章读取与线索创建服务

**Files:**
- Create: `app/services/wechat_leads.py`
- Test: `tests/test_wechat_leads.py`

**Interfaces:**
- Produces: `import_public_wechat_article(session, source, url, client) -> Job`。
- Consumes: `Source`、`Job`、`suggest_notice_type`。

- [ ] **Step 1: 写失败测试**

```python
def test_import_public_wechat_article_creates_pending_verification_lead(session):
    job = import_public_wechat_article(session, huazhi_source, ARTICLE_URL, FakeClient(ARTICLE_HTML))
    assert job.status == "待核验"
    assert job.official_url == ""
    assert "须补充官方原文" in job.risk_flags

def test_import_rejects_non_wechat_url(session):
    with pytest.raises(ValueError, match="公众号公开文章链接"):
        import_public_wechat_article(session, huazhi_source, "https://example.com/x", FakeClient(""))
```

- [ ] **Step 2: 运行测试，确认因模块不存在失败**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_wechat_leads.py -q`

- [ ] **Step 3: 最小实现**

实现 URL 白名单、`HTMLParser` 标题/正文提取、推广过滤、URL 指纹去重和 `Job` 创建；禁止网页读取失败或纯推广内容入库。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_wechat_leads.py -q`

### Task 3: 导入页面与路由

**Files:**
- Modify: `app/main.py`
- Create: `app/templates/wechat_lead_import.html`
- Modify: `app/templates/sources.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `GET /sources/wechat-leads/import` 和 `POST /sources/wechat-leads/import`。

- [ ] **Step 1: 写失败测试**

```python
def test_wechat_lead_import_page_is_available(client):
    response = client.get("/sources/wechat-leads/import")
    assert response.status_code == 200
    assert "公众号招聘线索导入" in response.text
```

- [ ] **Step 2: 运行测试，确认因路由不存在失败**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_web.py::test_wechat_lead_import_page_is_available -q`

- [ ] **Step 3: 最小实现**

增加链接表单、来源页入口、读取失败的明确错误提示；成功后跳转到新建待核验岗位详情页。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_web.py::test_wechat_lead_import_page_is_available -q`

### Task 4: 全量验证与台账

**Files:**
- Modify: `README.md`
- Modify: `项目管理/项目推进台账.xlsx`

- [ ] **Step 1: 更新说明**

在 README 明确“上海华智公考是人工提供公开文章链接的线索源，不能自动发现文章列表，也不能自动发布”。

- [ ] **Step 2: 更新台账**

添加本次“上海华智公考公众号线索导入”更新摘要。

- [ ] **Step 3: 全量验证**

Run: `.venv\\Scripts\\python.exe -m pytest -q`

Expected: 全部测试通过；新增测试覆盖 URL、过滤、去重、待核验状态与页面入口。
