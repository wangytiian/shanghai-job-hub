from dataclasses import dataclass
import json
import re
from typing import Callable


@dataclass(frozen=True)
class IntakeScreeningResult:
    grade: str
    route: str
    reason: str
    evidence: str
    confidence: str


_PROGRESS_TERMS = ("体检", "面试通知", "拟录取", "录用公示", "录取公示", "入职报到")
_FILTER_TERMS = ("收费招聘", "培训贷")
_SENIOR_TERMS = ("副教授", "正教授", "博士后", "高级职称", "负责人", "总经理", "行长", "总监", "三年以上", "3年以上", "50岁以上", "五十岁以上")
_A_TERMS = ("实习", "应届", "校招", "校园招聘", "管培生", "毕业两年")
_APPLICATION_TERMS = ("简历投递", "投递简历", "报名方式", "报名时间", "报名截止", "应聘方式", "申请方式", "官方投递邮箱", "官方报名入口", "官方投递入口")
REVIEW_NOTE = "受众待确认：原文未明确应届生或在校生可投，须人工核实后再决定是否分发。[TARGET_AUDIENCE_UNCLEAR]"
_JOB_BODY_TERMS = ("岗位职责", "工作职责", "工作内容", "职位描述")
_JOB_REQUIREMENT_TERMS = ("任职要求", "招聘要求", "岗位要求", "资格条件")
_JUNIOR_TERMS = ("本科", "大专", "专科", "学历不限", "经验不限", "专员", "助理", "初级")
_GENERIC_TITLE = re.compile(r"^(?:社会招聘|招聘公告|招聘简章|招聘信息|岗位汇总|岗位列表)(?:公告|信息)?$")
_ASSISTING_SENIOR = re.compile(r"(?:协助|配合)(?:部门|团队|项目)?(?:负责人|总经理|行长|总监)")
_ROUTES = {"A": "优先待核验", "B": "普通待核验", "C": "人工复核", "D": "过滤留档"}


def _has_specific_junior_role(title: str, evidence_text: str) -> bool:
    text = f"{title}\n{evidence_text}"
    return (
        bool(title.strip())
        and not _GENERIC_TITLE.fullmatch(title.strip())
        and any(term in evidence_text for term in _JOB_BODY_TERMS)
        and any(term in evidence_text for term in _JOB_REQUIREMENT_TERMS)
        and any(term in text for term in _JUNIOR_TERMS)
        and any(term in evidence_text for term in _APPLICATION_TERMS)
    )


def screen_intake(title: str, evidence_text: str) -> IntakeScreeningResult:
    """Conservative A/B/C/D screening; uncertain content is never discarded."""
    hard_result = _hard_filter(title, evidence_text)
    if hard_result is not None:
        return hard_result
    text = f"{title}\n{evidence_text}".strip()
    for term in _A_TERMS:
        if term in text:
            return IntakeScreeningResult("A", "优先待核验", f"原文出现“{term}”学生或初级岗位信号。", term, "高")
    if _has_specific_junior_role(title, evidence_text):
        return IntakeScreeningResult("B", "普通待核验", REVIEW_NOTE, "具体岗位及投递方式", "中")
    return IntakeScreeningResult("C", "人工复核", "原文缺少明确学生适配信号，保留给人工复核。", "未发现直接适配证据", "低")


def build_intake_screening_prompt(title: str, evidence_text: str) -> str:
    return f"""你是学生招聘线索初筛助手。只根据原文判断 A/B/C/D，返回纯 JSON：grade, reason, evidence, confidence。
A=实习、校招、应届或毕业两年内初级岗位；B=可能适配但需要人工确认；C=原文不完整或适配不明；D=体检、面试、录用等进度通知，或明确高职称、负责人、三年以上经验、年龄偏高、收费招聘等。
标题为进度通知仍判D；学生招聘提供明确投递或报名说明时，正文的后续体检、面试等流程安排不能单独作为D级依据。收费、培训贷或明确高级资历要求仍判D。
evidence 必须是原文中连续出现的短语。不得推断岗位条件。grade 只能是 A/B/C/D，confidence 只能是 高/中/低。
标题：{title}\n原文：{evidence_text[:6000]}"""


def _hard_filter(title: str, evidence_text: str) -> IntakeScreeningResult | None:
    text = f"{title}\n{evidence_text}".strip()
    for term in _FILTER_TERMS:
        if term in text:
            return IntakeScreeningResult("D", "过滤留档", f"原文出现“{term}”，不适合学生招聘入库。", term, "高")
    # Assisting a manager is a junior duty, not a requirement to be that manager.
    senior_text = f"{title}\n{_ASSISTING_SENIOR.sub('', evidence_text)}"
    for term in _SENIOR_TERMS:
        if term in senior_text:
            return IntakeScreeningResult("D", "过滤留档", f"原文出现“{term}”，不适合学生招聘入库。", term, "高")
    has_student_application = any(term in text for term in _A_TERMS) and any(
        term in text for term in _APPLICATION_TERMS
    )
    progress_text = title if has_student_application else text
    for term in _PROGRESS_TERMS:
        if term in progress_text:
            return IntakeScreeningResult("D", "过滤留档", f"原文出现“{term}”，不适合学生招聘入库。", term, "高")
    return None


def screen_intake_with_ai(
    title: str,
    evidence_text: str,
    complete: Callable[[str], str],
) -> IntakeScreeningResult:
    """Use the model only after hard filtering; model errors never promote a lead."""
    hard_result = _hard_filter(title, evidence_text)
    if hard_result is not None:
        return hard_result
    try:
        content = complete(build_intake_screening_prompt(title, evidence_text))
        payload = json.loads(content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        grade = str(payload.get("grade") or "").strip().upper()
        reason = str(payload.get("reason") or "").strip()
        evidence = str(payload.get("evidence") or "").strip()
        confidence = str(payload.get("confidence") or "").strip()
        context = f"{title}\n{evidence_text}"
        if grade not in _ROUTES or confidence not in {"高", "中", "低"} or not evidence or evidence not in context:
            raise ValueError("AI 返回的分级或证据不可核验")
        if grade == "B" and not _has_specific_junior_role(title, evidence_text):
            return IntakeScreeningResult(
                "C", "人工复核", "AI 建议 B，但岗位级职责、初级条件或投递方式证据不足。", evidence, "低"
            )
        return IntakeScreeningResult(
            grade,
            _ROUTES[grade],
            REVIEW_NOTE if grade == "B" else reason or "AI 根据原文给出的初筛建议。",
            evidence,
            confidence,
        )
    except Exception:
        return IntakeScreeningResult(
            "C", "人工复核", "AI 初筛不可用或证据无法核验，保守转人工复核。", "AI 初筛未完成", "低"
        )
