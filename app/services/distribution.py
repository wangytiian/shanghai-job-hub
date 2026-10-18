from dataclasses import dataclass
from datetime import date
import ast
import json
from html import escape
from types import SimpleNamespace
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DistributionItem, Job
from app.services.ai_content_draft import ContentDraft
from app.services.jobs import UNSPECIFIED_DEADLINE
from app.services.jobs import validate_publishable
from app.services.screenshot_contracts import ScreenshotFacts
from app.services.screenshot_contracts import EvidenceRef
from app.services.deadline_policy import parse_known_deadline
from app.services.screenshot_review_policy import has_current_review_confirmation, validate_review_confirmation


NON_OFFICIAL_NOTICE = "本内容为面向上海立信会计金融学院学生的非官方就业信息服务；请以官方原文为准。"
PLACEHOLDER_VALUES = {"以公告原文为准", "待人工判断", "待分类", "待核验", "原文待人工确认", "原文未明确"}
INTERNAL_NOTICE = "内部草稿 · 未审核"
_RENDER_FIELDS = (
    "employer_name", "job_title", "announcement_title", "recruitment_type", "location_detail",
    "target_audience", "direction_tags", "deadline", "official_url", "source_url", "posting_scope",
    "application_method", "application_contact", "salary", "role_summary", "eligibility", "source_name",
)


def _http_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 33 or char in '<>"\\' for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        parsed.port  # Reject malformed ports as well as malformed hosts.
    except ValueError:
        return ""
    return value


def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("已确认事实快照包含重复字段")
        result[key] = value
    return result


def _validate_snapshot(data):
    required = {"schema_version", "import_id", "content_revision", "fields", "roles", "common_requirements", "evidence_refs"}
    if not isinstance(data, dict) or not required <= set(data) or set(data) - required - {"student_roles_excerpt", "review_confirmation"}:
        raise ValueError("已确认事实快照结构无效")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("已确认事实快照版本无效")
    if any(type(data[name]) is not int or data[name] < 1 for name in ("import_id", "content_revision")):
        raise ValueError("已确认事实快照来源版本无效")
    def plain_fields(value):
        return isinstance(value, dict) and len(value) <= 100 and all(
            isinstance(key, str) and len(key) <= 100 and isinstance(item, str) and len(item) <= 24000
            for key, item in value.items())
    if not plain_fields(data["fields"]) or not isinstance(data["roles"], list) or len(data["roles"]) > 100:
        raise ValueError("已确认事实快照字段无效")
    if any(not plain_fields(role) or not role.get("job_title", "").strip() for role in data["roles"]):
        raise ValueError("已确认事实快照岗位名称或条件无效")
    common, refs = data["common_requirements"], data["evidence_refs"]
    if not isinstance(common, list) or len(common) > 100 or any(not isinstance(item, str) or len(item) > 24000 for item in common):
        raise ValueError("已确认事实快照公共条件无效")
    if not isinstance(refs, list) or len(refs) > 5000 or any(not isinstance(ref, dict) for ref in refs):
        raise ValueError("已确认事实快照证据无效")
    try:
        for ref in refs:
            EvidenceRef.from_dict(ref)
    except ValueError as exc:
        raise ValueError("已确认事实快照证据结构无效") from exc
    if type(data.get("student_roles_excerpt", False)) is not bool:
        raise ValueError("已确认事实快照岗位节选标识无效")
    if "review_confirmation" in data:
        cert = data["review_confirmation"]
        if not isinstance(cert, dict) or not validate_review_confirmation(data, job_id=cert.get("job_id"), job_version=cert.get("job_version")):
            raise ValueError("截图一次终审证明无效，请重新核验")
    return data


def build_confirmed_content_snapshot(facts: ScreenshotFacts, *, import_id: int,
                                     content_revision: int, student_roles_excerpt: bool = False) -> dict:
    """Flatten already confirmed facts; keep all per-role conditions/provenance."""
    if not isinstance(facts, ScreenshotFacts):
        raise ValueError("已确认事实快照需要 ScreenshotFacts")
    if facts.content_revision != content_revision:
        raise ValueError("已确认事实快照内容版本与提取事实不一致")
    refs = []
    seen = set()
    def plain_fields(fields):
        result = {}
        for key, field in fields.items():
            result[key] = field.value
            for ref in field.evidence_refs:
                encoded = json.dumps(ref.to_dict(), ensure_ascii=False, sort_keys=True)
                if encoded not in seen:
                    refs.append(ref.to_dict())
                    seen.add(encoded)
        return result
    fields = plain_fields(facts.fields)
    if "deadline" in facts.fields:
        fields["deadline_state"] = facts.fields["deadline"].state
        fields["deadline_original_text"] = facts.fields["deadline"].value
    roles = [plain_fields(role) for role in facts.roles]
    common = []
    for field in facts.common_requirements:
        plain_fields({"common_requirement": field})
        if field.value:
            common.append(field.value)
    return _validate_snapshot({"schema_version": 1, "import_id": import_id, "content_revision": content_revision,
        "fields": fields, "roles": roles, "common_requirements": common, "evidence_refs": refs,
        "student_roles_excerpt": student_roles_excerpt})


def load_confirmed_snapshot(job: Job, *, mode: str = "verified") -> dict | None:
    if mode not in {"internal", "verified"}:
        raise ValueError("未知的招聘内容模式")
    raw = getattr(job, "confirmed_content_json", "")
    version = getattr(job, "confirmed_content_version", 0)
    if not raw:
        if version:
            raise ValueError("已确认事实快照缺失或过期，请重新核验")
        return None
    if mode == "verified" and version != job.version:
        raise ValueError("已确认事实快照已过期，请按当前事实重新核验")
    if not isinstance(raw, str) or len(raw) > 4_000_000:
        raise ValueError("已确认事实快照无效")
    try:
        snapshot = _validate_snapshot(json.loads(raw, object_pairs_hook=_unique_json_pairs))
        if mode == "verified" and "review_confirmation" in snapshot and not has_current_review_confirmation(job):
            raise ValueError("截图一次终审证明已失效，请重新核验")
        return snapshot
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("已确认事实快照无法读取") from exc


def _render_context(job, mode):
    snapshot = load_confirmed_snapshot(job, mode=mode)
    fields = snapshot["fields"] if snapshot else {name: getattr(job, name, "") or "" for name in _RENDER_FIELDS}
    source = SimpleNamespace(**{name: fields.get(name, "") for name in _RENDER_FIELDS})
    source.official_url = _http_url(source.official_url)
    source.source_url = _http_url(source.source_url)
    return source, snapshot


def distribution_blockers(job: Job, item: DistributionItem | None = None) -> list[str]:
    """Formal output gate; keep approval-time validate_publishable independent."""
    errors = []
    if job.status != "可发布":
        errors.append("只有可发布岗位可以生成或复制分发内容")
    if job.lifecycle_status != "正常":
        errors.append("岗位生命周期已变化，请重新核验")
    errors.extend(validate_publishable(job))
    if job.distribution_recommendation != "进入学生分发审核":
        errors.append("该岗位不适合核心学生用户或仅保留资料库，不生成学生渠道内容")
    try:
        source, snapshot = _render_context(job, "verified")
        if not source.official_url or not source.source_url:
            errors.append("来源或官方链接必须为合法 HTTP(S) 地址")
        if snapshot:
            deadline = parse_known_deadline(source.deadline)
            if deadline is not None and deadline < date.today():
                errors.append("已确认事实快照的报名时间已截止")
    except ValueError as exc:
        errors.append(str(exc))
    if item is not None:
        if item.job_id != job.id:
            errors.append("分发内容不属于当前岗位")
        if item.job_version != job.version:
            errors.append("分发草稿版本已过期，请重新生成或人工确认")
        if item.status in {"待确认", "待重新确认", "已失效"}:
            errors.append("分发草稿仍待人工确认")
    return list(dict.fromkeys(errors))


def filter_current_queue_items(session: Session, items: list[DistributionItem]) -> list[DistributionItem]:
    """Never expose copy-ready content for a job that is no longer publishable."""
    visible: list[DistributionItem] = []
    for item in items:
        job = session.get(Job, item.job_id)
        if job is None:
            continue
        if not distribution_blockers(job, item):
            visible.append(item)
    return visible


@dataclass(frozen=True)
class WechatDraft:
    title: str
    digest: str
    html: str
    plain_text: str


def _known(value: str) -> bool:
    return bool(value.strip()) and value.strip() not in PLACEHOLDER_VALUES


def _line(label: str, value: str) -> str:
    if not _known(value):
        return ""
    return f'<p style="margin:7px 0;"><strong style="display:inline-block;min-width:92px;color:#102A43;">{escape(label)}</strong>｜{escape(value)}</p>'


def _display_value(value: str) -> str:
    """Keep imported JSON/Python list strings out of user-facing copy."""
    cleaned = value.strip()
    if not _known(cleaned):
        return ""
    if cleaned.startswith("[") and cleaned.endswith("]"):
        try:
            parsed = ast.literal_eval(cleaned)
        except (SyntaxError, ValueError):
            parsed = None
        if isinstance(parsed, (list, tuple)):
            return "、".join(str(item).strip() for item in parsed if str(item).strip())
    return cleaned


def _section_heading(title: str) -> str:
    return (
        '<div style="height:1px;margin:30px 0 24px;background:#E7E7E7;"></div>'
        '<p style="margin:0 0 2px;text-align:center;font-family:Georgia,serif;font-size:13px;letter-spacing:.04em;">FINJOB</p>'
        f'<h2 style="margin:0 0 20px;text-align:center;color:#111;font-size:25px;line-height:1.35;">{title}</h2>'
    )


def _fact_row(label: str, value: str) -> str:
    readable = _display_value(value)
    if not readable:
        return ""
    return (
        '<p style="margin:8px 0;font-size:16px;line-height:1.8;">'
        f'<strong style="display:inline-block;min-width:90px;color:#132D54;">{escape(label)}</strong>'
        f'<span style="display:inline-block;margin:0 12px;color:#1F2937;">｜</span>{escape(readable)}</p>'
    )


def _application_copy(job: Job) -> tuple[str, str]:
    url = _http_url(job.official_url)
    official_url = escape(url, quote=True)
    deadline_unstated = job.deadline.strip() == UNSPECIFIED_DEADLINE
    if not _known(job.deadline):
        deadline_instruction = "报名时间待补充，请对照完整官方公告确认"
    elif deadline_unstated:
        deadline_instruction = "公告未明确统一截止时间，建议尽快查看官方原文或附件确认报名安排"
    else:
        deadline_instruction = "请在截止日期前"
    source_html = f'<p style="margin:0;color:#667085;font-size:12px;word-break:break-all;">招聘单位官方公告：{official_url}</p>' if url else '<p>官方公告链接待补充</p>'
    source_text = f"招聘单位官方公告：{url}" if url else "官方公告链接待补充"
    if job.application_method == "email":
        contact = escape(job.application_contact)
        return (
            f'<p style="margin:0 0 10px;">{deadline_instruction}，按招聘单位公告要求准备材料。请将材料发送至：<strong>{contact}</strong>。</p>'
            f'{source_html}',
            f"投递方式：邮件投递\n报名邮箱：{job.application_contact or '待补充'}\n{source_text}",
        )
    if not _known(job.deadline):
        instruction = "报名时间待补充，请对照完整官方公告确认投递安排。"
    elif deadline_unstated:
        instruction = "公告未明确统一截止时间，建议尽快查看官方原文或附件确认报名安排。"
    elif job.application_method == "official_platform":
        instruction = "请在截止日期前进入招聘单位官方招聘平台完成申请。"
    elif job.application_method == "on_site":
        instruction = "请按招聘单位官方公告的时间、地点和材料要求现场报名。"
    else:
        instruction = "请在截止日期前通过招聘单位官方公告所示入口完成申请。"
    if job.application_method == "official_platform":
        instruction += " 投递方式：官方招聘平台。"
    elif job.application_method == "on_site":
        instruction += " 投递方式：现场报名。"
    if job.application_contact:
        instruction += f" 投递说明：{job.application_contact}"
    button = f'<p style="margin:14px 0;text-align:center;"><a href="{official_url}" style="display:inline-block;padding:10px 20px;border-radius:5px;background:#0B4A87;color:#FFFFFF;text-decoration:none;font-weight:700;">查看招聘单位官方公告</a></p>' if url else ""
    return (
        f'<p style="margin:0 0 10px;">{escape(instruction)}</p>{button}{source_html}',
        f"投递方式：{instruction}\n{source_text}",
    )


def _snapshot_role_text(snapshot):
    if not snapshot:
        return ""
    labels = {"job_title": "岗位", "role_summary": "职责", "responsibilities": "职责", "eligibility": "资格条件",
              "requirements": "资格条件", "salary": "薪资", "target_audience": "适合人群", "direction_tags": "专业方向",
              "location_detail": "工作地点", "application_contact": "投递说明", "application_method": "投递方式",
              "deadline": "申请截止", "recruitment_type": "招聘类型", "education": "学历", "major": "专业要求",
              "graduation_year": "毕业届别", "experience": "经验要求", "official_url": "官方链接", "source_url": "来源链接"}
    metadata = {"notice_type", "posting_scope", "attachment_status", "student_fit_level", "distribution_recommendation",
                "confidence", "rationale", "role_details_status", "role_details_evidence", "evidence_snippet", "location_category"}
    paragraphs = []
    for role in snapshot["roles"]:
        lines = [f"岗位：{role['job_title']}"]
        for name, value in role.items():
            if name == "job_title" or name in metadata or name.endswith("_state") or not _known(value):
                continue
            lines.append(f"{labels.get(name, '岗位补充说明')}：{_display_value(value)}")
        paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs)


def build_wechat_draft(job: Job, content_draft: ContentDraft | None = None, *, mode: str = "verified") -> WechatDraft:
    """Render the confirmed FINJOB-style, clipboard-ready WeChat article."""
    job, snapshot = _render_context(job, mode)
    employer = job.employer_name
    job_title = job.job_title
    source_url = escape(job.source_url, quote=True)
    is_summary = job.posting_scope == "multi_role_announcement"
    audience = _display_value(job.target_audience)
    directions = _display_value(job.direction_tags)
    location = _display_value(job.location_detail) or ("待补充" if mode == "internal" else "")
    deadline = _display_value(job.deadline) or ("待补充" if mode == "internal" else "")
    ai_content = content_draft or ContentDraft()
    opening = (
        f"{employer}发布了{job_title}招聘信息。该公告包含多个岗位，请逐岗对照职责、条件与投递要求。"
        if is_summary
        else f"{employer}正在招聘{job_title}。以下内容依据已核验的公开招聘事实整理，投递前请再次查看官方公告。"
    )
    if mode == "internal":
        opening = f"{employer or '招聘单位待补充'} {job_title or '岗位待补充'}，请对照原始材料核验以下内容。"
    elif not snapshot:
        opening = ai_content.company_intro or opening
    excerpt = bool(snapshot and snapshot.get("student_roles_excerpt"))
    if excerpt:
        opening = "学生岗位节选。" + opening
    overview = "".join((
        _fact_row("招聘单位", job.employer_name),
        _fact_row("招聘岗位", job.job_title),
        _fact_row("招聘类型", job.recruitment_type),
        _fact_row("工作地点", location),
        _fact_row("申请截止", deadline),
        _fact_row("薪资", job.salary),
    ))
    common_text = "\n".join(f"• {item}" for item in snapshot["common_requirements"] if item) if snapshot else ""
    factual_eligibility = job.eligibility if snapshot else ai_content.eligibility
    eligibility = "".join((
        f'<p style="margin:8px 0;">• 适合人群：{escape(audience)}</p>' if audience else "",
        f'<p style="margin:8px 0;">• 专业方向：{escape(directions)}</p>' if directions else "",
        f'<p style="margin:8px 0;white-space:pre-line;">• {escape(factual_eligibility)}</p>' if factual_eligibility else "",
        f'<p style="margin:8px 0;white-space:pre-line;">公共条件：\n{escape(common_text)}</p>' if common_text else "",
    ))
    application_html, application_text = _application_copy(job)
    role = ("\n\n".join(filter(None, (job.role_summary, _snapshot_role_text(snapshot)))) if snapshot else ai_content.role_summary) or (
        f"岗位方向为{directions}。请查看官方原文，确认具体职责、部门安排及岗位要求。"
        if directions else "请查看官方原文，确认具体职责、部门安排及岗位要求。"
    )
    career_advice = ai_content.career_advice or "投递前建议结合岗位方向，整理课程项目、实习或社团经历，突出与岗位相关的能力。"
    apply_tip = ai_content.apply_tip or "投递前请再次核对官方公告中的材料、入口和时间要求。"
    title_basis = job.announcement_title if is_summary and job.announcement_title else job.job_title
    draft_title = f"{job.employer_name}{title_basis}招聘"
    if excerpt:
        draft_title += "｜学生岗位节选"
    digest_parts = [job.recruitment_type]
    if _known(job.location_detail):
        digest_parts.append(job.location_detail)
    if _known(job.deadline):
        digest_parts.append(f"截止 {job.deadline}")
    digest = "｜".join(digest_parts)
    html = f'''<section style="max-width:677px;margin:0 auto;padding:10px 18px 32px;color:#161616;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;font-size:16px;line-height:1.9;box-sizing:border-box;">
  <h1 style="margin:0 0 8px;color:#111;font-size:30px;line-height:1.4;font-weight:700;letter-spacing:.01em;">{escape(draft_title)}</h1>
  <p style="margin:0 0 18px;color:#6B7D9B;font-size:14px;">沪上求职汇　｜　{INTERNAL_NOTICE if mode == 'internal' else '已核验后生成'}</p>
  <p style="margin:0 0 24px;">{escape(opening)}</p>
  {_section_heading("招聘速览").replace('FINJOB', 'OVERVIEW')}
  {overview}
  {_section_heading("岗位内容").replace('FINJOB', 'ROLE')}
  <p style="margin:0;white-space:pre-line;">{escape(role)}</p>
  {_section_heading("申请条件").replace('FINJOB', 'ELIGIBILITY') if eligibility else ""}
  {eligibility}
  {_section_heading("求职建议").replace('FINJOB', 'CAREER GUIDE')}
  <p style="margin:0;">{escape(career_advice)}</p>
  {_section_heading("如何投递").replace('FINJOB', 'APPLY')}
  <p style="margin:0 0 12px;">{escape(apply_tip)}</p>
  {application_html}
  <div style="height:1px;margin:28px 0 14px;background:#E7E7E7;"></div>
  <p style="margin:5px 0;color:#7B869A;font-size:12px;">信息来源｜{f'<a href="{source_url}" style="color:#7B869A;">公开原文</a>' if source_url else '来源链接待补充'} {escape(job.source_name)}</p>
  <p style="margin:5px 0;color:#7B869A;font-size:12px;">{INTERNAL_NOTICE if mode == 'internal' else '核验时间｜以本次人工审核记录为准'}</p>
  <div style="height:1px;margin:18px 0;background:#E7E7E7;"></div>
  <p style="margin:0;text-align:center;color:#667085;font-size:12px;">面向青年求职者的非官方就业信息服务</p>
</section>'''
    plain_lines = [draft_title, f"沪上求职汇｜{INTERNAL_NOTICE if mode == 'internal' else '已核验后生成'}", "", opening, "", "【招聘速览】"]
    for label, value in (("招聘单位", job.employer_name), ("招聘岗位", job.job_title), ("招聘类型", job.recruitment_type), ("工作地点", location), ("申请截止", deadline), ("薪资", job.salary)):
        readable = _display_value(value)
        if readable:
            plain_lines.append(f"{label}｜{readable}")
    if eligibility:
        plain_lines.extend(("", "【申请条件】"))
        if audience:
            plain_lines.append(f"• 适合人群：{audience}")
        if directions:
            plain_lines.append(f"• 专业方向：{directions}")
        if factual_eligibility:
            plain_lines.append(f"• {factual_eligibility}")
        if common_text:
            plain_lines.append(f"公共条件：\n{common_text}")
    plain_lines.extend(("", "【岗位内容】", role, "", "【求职建议】", career_advice, "", "【如何投递】", apply_tip, application_text,
        "", f"信息来源：{job.source_url or '待补充'} {job.source_name}", "", NON_OFFICIAL_NOTICE))
    return WechatDraft(title=draft_title, digest=digest, html=html, plain_text="\n".join(plain_lines))


def _public_article(job: Job) -> str:
    return build_wechat_draft(job).plain_text


def build_group_message(job: Job, *, mode: str = "verified") -> str:
    job, snapshot = _render_context(job, mode)
    location = _display_value(job.location_detail)
    deadline = (
        "公告未明确统一截止时间，建议尽快查看官方原文或附件确认报名安排"
        if job.deadline.strip() == UNSPECIFIED_DEADLINE
        else _display_value(job.deadline) or "待补充"
    )
    lines = [f"【{_display_value(job.recruitment_type) or '招聘'}】{job.employer_name} {job.job_title}"]
    if mode == "internal":
        lines.insert(0, INTERNAL_NOTICE)
    if snapshot and snapshot.get("student_roles_excerpt"):
        lines.append("学生岗位节选")
    for label, value in (("地点", location), ("适合人群", job.target_audience), ("专业方向", job.direction_tags),
                         ("资格条件", job.eligibility), ("薪资", job.salary)):
        readable = _display_value(value)
        if readable:
            lines.append(f"{label}：{readable}")
    role_text = "\n\n".join(filter(None, (job.role_summary, _snapshot_role_text(snapshot))))
    if role_text:
        lines.append(role_text)
    if snapshot:
        lines.extend(f"公共条件：{item}" for item in snapshot["common_requirements"] if item)
    _, application_text = _application_copy(job)
    lines.extend((application_text, f"报名时间：{deadline}", f"信息来源：{job.source_url or '待补充'} {job.source_name}", NON_OFFICIAL_NOTICE))
    return "\n".join(lines)


def _group_message(job: Job) -> str:
    """Compatibility for existing callers."""
    return build_group_message(job)


def create_distribution_items(session: Session, job_id: int, *, commit: bool = True) -> list[DistributionItem]:
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError("岗位不存在")
    errors = distribution_blockers(job)
    if errors:
        raise ValueError("；".join(errors))
    wanted = (("公众号", "公众号草稿", _public_article(job)), ("微信群", job.target_audience, _group_message(job)))
    items: list[DistributionItem] = []
    for channel, audience_group, content in wanted:
        item = session.scalar(select(DistributionItem).where(DistributionItem.job_id == job.id, DistributionItem.channel == channel))
        if item is None:
            item = DistributionItem(
                job_id=job.id,
                channel=channel,
                audience_group=audience_group,
                content=content,
                job_version=job.version,
                template_version="finjob-v1",
            )
            session.add(item)
        else:
            if item.is_manually_edited:
                if item.job_version != job.version:
                    item.ai_content_error = "岗位事实已更新，人工精修原稿已失效，请核对当前事实后重新确认。"
                    item.status = "待重新确认"
                items.append(item)
                continue
            item.audience_group, item.content = audience_group, content
            if item.job_version != job.version:
                item.job_version = job.version
                item.template_version = "finjob-v1"
                item.ai_content_json = ""
                item.ai_content_status = "基础稿"
                item.ai_content_error = "岗位事实已更新，原 AI 内容已失效，请重新提炼。"
                item.status = "待发送"
        items.append(item)
    if commit:
        session.commit()
    else:
        session.flush()
    return items
