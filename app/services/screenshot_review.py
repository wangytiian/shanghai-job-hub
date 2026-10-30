"""Pure, deterministic readiness checks for a human screenshot review.

The caller supplies selected roles and the effective material completeness view.
An initial page is never blocked by its as-yet unclicked final acknowledgement;
the mutation boundary must check that explicit boolean itself.
"""
from dataclasses import asdict, dataclass, replace
from datetime import date
import re
from urllib.parse import urlsplit

from app.services.screenshot_contracts import ExtractedField, ScreenshotFacts


UNSPECIFIED_DEADLINE = "公告未明确统一截止时间"
DIAGNOSTIC_FIELDS = frozenset({"evidence", "confidence", "rationale", "uncertainties"})
OPTIONAL_FIELDS = frozenset({"salary", "role_summary", "direction_tags", "job_family", "announcement_title",
    "location_category", "recruitment_type"})
PLACEHOLDERS = frozenset({"以公告原文为准", "待人工判断", "待分类", "待核验", "原文待人工确认", "原文未明确", "地区待定"})


@dataclass(frozen=True)
class ReviewIssue:
    path: str
    code: str
    message: str
    severity: str
    action: str

    def to_dict(self) -> dict:
        return asdict(self)


def valid_http_url(value) -> bool:
    """A usable HTTP(S) link, without credentials, control characters or bad ports."""
    if not isinstance(value, str) or not value or len(value) > 500 or any(ord(c) < 33 or c in '<>"\\' for c in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username or parsed.password:
            return False
        parsed.port
        return not any(c.isspace() for c in host)
    except ValueError:
        return False


def known_deadline(value):
    """Parse full dated facts without supplying or guessing a year."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(20\d{2})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*日?\s*(?:(?:[T ]\d{1,2}:\d{2})(?::\d{2})?)?\s*", value)
    if not match:
        return None
    try:
        return date(*(int(value) for value in match.groups()))
    except ValueError:
        return None


def publication_facts(facts) -> ScreenshotFacts:
    """Drop diagnostics and safely absent optional facts, preserving all evidence.

    Nonempty uncertain candidates and every qualification/common restriction stay
    present so callers cannot use projection as conflict resolution.
    """
    def project(fields):
        return {name: field for name, field in fields.items() if name not in DIAGNOSTIC_FIELDS
            and not (name in OPTIONAL_FIELDS and field.state in {"missing", "unclear", "explicit"} and not field.value.strip())}
    return replace(facts, fields=project(facts.fields), roles=tuple(project(role) for role in facts.roles))


def _text(record):
    base = getattr(record, "edited_text", "") or getattr(record, "ocr_text", "") or ""
    return "\n".join(value for value in (base, getattr(record, "supplemental_text", "") or "") if isinstance(value, str))


def _graduate_only(value):
    # Interpret degree mentions inside their own clause: a doctoral preference
    # cannot cancel a master's minimum in the preceding clause (or create one).
    for clause in re.split(r"[，,。；;\n]", value):
        if not re.search(r"硕士|博士|研究生", clause):
            continue
        if re.search(r"(?:不限|不要求|无需|不限制).{0,8}(?:硕士|博士|研究生)|(?:硕士|博士|研究生).{0,6}(?:优先|优选|为佳|更佳|不限)", clause):
            continue
        if not re.search(r"本科|学士|专科|大专", clause):
            return True
    return False


def _fee_required(text):
    for line in re.split(r"[。；;\n]|但是|然而|但|另需", text):
        if any(term in line for term in ("不收费", "不得收费", "无需缴费", "不收取", "免费报名", "不需缴费", "无需支付", "不缴费")):
            continue
        if re.search(r"收费招聘|付费内推|付费求职|(?:报名|招聘|应聘|求职).{0,15}(?:缴纳|缴费|收费|支付).{0,10}(?:费|元)|(?:需|须|应).{0,8}缴纳.{0,8}(?:报名费|培训费)", line):
            return True
    return False


def _original_deadline(text):
    """Read only labelled deadline/window evidence, respecting explicit updates.

    Historical labels are superseded only by an actual current dated deadline.
    Competing current dates remain unresolved; opening and qualification dates
    are not deadlines. Returns (problem code, effective current date).
    """
    candidates, historical, unresolved, unresolved_update = [], False, False, False
    date_pattern = r"20\d{2}\s*(?:年|[-/.])\s*\d{1,2}\s*(?:月|[-/.])\s*\d{1,2}\s*日?"
    for clause in re.split(r"[。；;\n]", text):
        # Explicit absence is not a damaged date. Match the whole clause only:
        # a no-deadline statement must not hide a real restriction beside it.
        if re.fullmatch(r"\s*(?:公告未明确统一截止(?:时间|日期)|(?:报名)?截止(?:时间|日期)\s*[：:]\s*(?:未明确|未公布|未提供|未注明))\s*", clause):
            continue
        deadline_label = re.search(r"(?:报名|报考|申请|投递|应聘).{0,8}(?:截止|截至|结束)|截止日期|截止时间", clause)
        window_label = re.search(r"(?:报名|报考|申请|投递|应聘)(?:时间|期限)\s*[：:]?", clause)
        if not deadline_label and not window_label:
            continue
        is_update = bool(re.search(r"最新|现(?:已)?(?:报名|报考|申请|投递|应聘)?(?:截止|延长)|延长至|延期至|调整至|变更为", clause))
        matches = list(re.finditer(date_pattern, clause))
        if not deadline_label:
            # A single opening date and unrelated dates in submitted materials
            # must not be promoted into a closing date.
            if len(matches) < 2 or not re.search(r"至|到|—|~|～", clause[matches[0].end():matches[-1].start()]):
                if re.search(r"\d{1,2}月\d{1,2}日\s*(?:至|到|—|~|～)\s*\d{1,2}月\d{1,2}日", clause):
                    if is_update:
                        unresolved_update = True
                    else:
                        unresolved = True
                continue
        is_historical = bool(re.search(r"(?:原|原定|此前|旧)(?:报名|报考|申请|投递|应聘|截止)", clause))
        # "由旧日延长至新日" is one update, not historical evidence.
        if is_historical and not (len(matches) > 1 and is_update):
            historical = True
            continue
        dates = [known_deadline(match.group(0)) for match in matches]
        if not dates or any(value is None for value in dates):
            if is_update:
                unresolved_update = True
            else:
                unresolved = True
            continue
        # In a plain labelled deadline the first date is the deadline; later
        # dates can describe certificates/materials. Only explicit windows or
        # updates establish the last date as the new end.
        candidates.append((dates[-1] if is_update or not deadline_label else dates[0], is_update))
    updates = {value for value, is_update in candidates if is_update}
    current = updates or {value for value, _ in candidates}
    if len(current) > 1:
        return "deadline_conflict", None
    # A complete explicit latest/extension date can supersede an older
    # yearless clause. A malformed latest update must never borrow an old year.
    if unresolved_update or (unresolved and not updates) or (historical and not current):
        return "deadline_unresolved", None
    deadline = next(iter(current), None)
    return ("deadline_expired" if deadline is not None and deadline < date.today() else None), deadline


def _raw_target_constraints(text, roles):
    """Keep global/common requirements without borrowing an unselected role."""
    labels = r"(?:招聘对象|招聘学历|学历要求|招聘要求|应聘要求)\s*[：:]\s*([^。；;\n]+)"
    if not roles:
        return [match.group(1) for match in re.finditer(labels, text)]
    # Role qualification maps supplied by the caller are the selected subset.
    # Only announcement preamble and explicitly global clauses supplement them.
    boundaries = [match.start() for match in re.finditer(r"(?:岗位|职位)(?:[一二三四五六七八九十\d]+|[：:])", text)]
    boundaries.extend(text.find(role["job_title"].value) for role in roles if role.get("job_title") and role["job_title"].value in text)
    preamble = text[:min(boundaries)] if boundaries else ""
    constraints = [match.group(1) for match in re.finditer(labels, preamble)]
    constraints.extend(match.group(1) for match in re.finditer(r"(?:公共条件|通用条件|共同条件|所有岗位(?:要求|学历要求)?|全体岗位(?:要求)?)\s*[：:]?\s*([^。；;\n]+)", text))
    return constraints


def collect_review_issues(record, facts, *, acknowledgement=False) -> tuple[ReviewIssue, ...]:
    """Return every known actionable issue; never access storage or call a model."""
    issues = []
    def add(path, code, message, action="请对照当前原文核对、补充并保存", severity="error"):
        if not any(item.path == path and item.code == code for item in issues):
            issues.append(ReviewIssue(path, code, message, severity, action))
    if not isinstance(facts, ScreenshotFacts):
        return (ReviewIssue("fields", "invalid_facts", "提取事实结构无效", "error", "请重新整理或手工补充"),)
    if type(facts.content_revision) is not int or facts.content_revision < 1:
        add("fields", "invalid_revision", "内容版本无效，请刷新")
    if getattr(record, "material_complete", False) is not True:
        add("material_complete", "material_incomplete", "材料尚不完整", "请补齐失败图片，或核对并补充完整原文")
    text = _text(record)
    if not text.strip():
        add("edited_text", "original_missing", "缺少可核对的完整原文", "请补充原文或重新识别图片")
    if not valid_http_url(getattr(record, "source_url", "")):
        add("source_url", "invalid_source_url", "缺少有效的来源链接", "请填写可追溯的 HTTP(S) 来源链接")

    def value(name):
        item = facts.fields.get(name)
        return item.value.strip() if isinstance(item, ExtractedField) and isinstance(item.value, str) else ""
    def require(name, label):
        if not value(name) or value(name) in PLACEHOLDERS:
            add(f"fields.{name}", "required", f"缺少明确的{label}")
    def inspect(fields, prefix):
        for name, item in fields.items():
            if name in DIAGNOSTIC_FIELDS:
                continue
            path = f"{prefix}.{name}"
            if not isinstance(item, ExtractedField) or not isinstance(item.value, str) or item.state not in {"missing", "unclear", "conflict", "explicit"}:
                add(path, "invalid_field", "字段结构或取值无效")
            elif item.state == "conflict" or (item.state == "unclear" and (name not in OPTIONAL_FIELDS or item.value.strip())):
                add(path, "unresolved_field", "存在字段冲突或不确定值", "请对照原文确定真实值；不要选择无依据的候选")
    inspect(facts.fields, "fields")
    for index, role in enumerate(facts.roles):
        inspect(role, f"roles.{index}")
        title = role.get("job_title")
        if not title or not title.value.strip() or title.value.strip() in PLACEHOLDERS:
            add(f"roles.{index}.job_title", "required", "所选岗位缺少具体名称")
    for index, item in enumerate(facts.common_requirements):
        if item.state != "explicit" or not item.value.strip():
            add(f"common_requirements.{index}", "common_unresolved", "公共资格或限制尚未核对")
    for name, label in (("employer_name", "单位"), ("location_detail", "工作地点"), ("target_audience", "学生受众")):
        require(name, label)
    if not facts.roles:
        require("job_title", "岗位名称")
    if not valid_http_url(value("official_url")):
        add("fields.official_url", "invalid_official_url", "缺少有效的官方投递链接", "请填写真实的官方 HTTP(S) 投递依据")
    method = value("application_method")
    if method not in {"email", "official_page", "official_platform", "on_site", "other"}:
        add("fields.application_method", "application_unknown", "投递方式尚未明确")
    if method == "email":
        contact = value("application_contact")
        local = contact.split("@", 1)[0]
        if len(contact) > 254 or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", contact):
            add("fields.application_contact", "invalid_email", "邮件投递缺少真实有效邮箱", "请对照原文填写完整邮箱")
    if method == "other" and not value("application_contact"):
        add("fields.application_contact", "application_missing", "其他投递方式缺少具体投递说明")
    deadline = value("deadline")
    parsed = known_deadline(deadline)
    if deadline != UNSPECIFIED_DEADLINE and parsed is None:
        add("fields.deadline", "deadline_unresolved", "截止日期格式无效或缺少年份", f"有日期时请依据原文填写完整年月日；原文未规定统一期限可留空，确认时保留“{UNSPECIFIED_DEADLINE}”")
    elif parsed is not None and parsed < date.today():
        add("fields.deadline", "deadline_expired", "报名已截止", "请核对官方最新有效日期；过期内容仅保留草稿")
    original_problem, original_deadline = _original_deadline(text)
    if original_problem == "deadline_expired":
        add("fields.deadline", original_problem, "原文明确当前报名已截止", "请核对官方最新有效日期；过期内容仅保留草稿")
    elif original_problem:
        add("fields.deadline", original_problem, "原文截止日期缺少年份或存在未解决的日期冲突", "请依据当前有效公告核对年份和延期依据后保存")
    elif original_deadline is not None and parsed is not None and original_deadline != parsed:
        add("fields.deadline", "deadline_conflict", "当前原文截止日期与已填日期不一致", "请对照当前有效公告及延期说明核对后保存")
    if value("posting_scope") not in {"single_role", "multi_role_announcement"}:
        add("fields.posting_scope", "scope_unresolved", "公告尚未明确具体岗位范围")
    elif value("posting_scope") == "multi_role_announcement" and not facts.roles:
        add("fields.posting_scope", "scope_unresolved", "多岗位公告缺少独立岗位条件", "请补充明细并选择有依据的岗位")
    if value("attachment_status") == "pending":
        add("fields.attachment_status", "attachment_pending", "必要附件尚未核对", "请补齐并核对岗位附件")
    if value("notice_type") != "新招聘":
        add("fields.notice_type", "notice_unconfirmed", "尚未确认为新招聘")
    if value("student_fit_level") not in {"核心适配", "补充适配"}:
        add("fields.student_fit_level", "audience_unconfirmed", "学生适配尚未确认或不适合学生")
    if value("distribution_recommendation") != "进入学生分发审核":
        add("fields.distribution_recommendation", "distribution_not_allowed", "当前建议仅保留资料库或不进入学生分发")
    targets = [value("target_audience")]
    if not facts.roles:
        targets.append(value("eligibility"))
    targets.extend(item.value for role in facts.roles for name, item in role.items() if name in {"target_audience", "eligibility"})
    targets.extend(item.value for item in facts.common_requirements)
    targets.extend(_raw_target_constraints(text, facts.roles))
    if value("student_fit_level") == "核心适配" and any(_graduate_only(target) for target in targets):
        add("fields.student_fit_level", "graduate_only", "硕士或博士限定岗位不能标记为本科核心适配", "请核对所选岗位学历要求并调整适配判断")
    if _fee_required(text) or any(_fee_required(item.value) for item in facts.common_requirements):
        add("common_requirements", "recruitment_fee", "存在收费招聘或报名缴费限制", "收费招聘不可进入学生分发，请保留资料库")
    critical = ("岗位边界", "归属", "多个公司", "多公司", "拆批", "多单位", "跨公司", "单位冲突", "无法区分", "AI公共条件待核对")
    for warning in facts.warnings:
        if any(term in warning for term in critical):
            add("fields.posting_scope" if "公共条件" not in warning else "common_requirements", "scope_unresolved", warning, "请补齐当前原文的单位、岗位及公共条件依据后保存")
    employers = set(re.findall(r"(?:招聘单位|用人单位|公司名称|单位名称)\s*[：:]\s*([^。；;\n，,]{2,80})", text))
    if len(employers) > 1:
        add("fields.posting_scope", "scope_unresolved", "原文包含多个单位，岗位归属须明确", "请选择单一单位的连续原文或拆分导入")
    return tuple(issues)
