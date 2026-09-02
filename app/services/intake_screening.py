from dataclasses import dataclass
import json
from typing import Callable


@dataclass(frozen=True)
class IntakeScreeningResult:
    grade: str
    route: str
    reason: str
    evidence: str
    confidence: str


_FILTER_TERMS = ("体检", "面试通知", "拟录取", "录用公示", "录取公示", "入职报到", "收费招聘", "培训贷")
_SENIOR_TERMS = ("副教授", "正教授", "博士后", "高级职称", "负责人", "总经理", "行长", "总监", "三年以上", "3年以上", "50岁以上", "五十岁以上")
_A_TERMS = ("实习", "应届", "校招", "校园招聘", "管培生", "毕业两年")
_ROUTES = {"A": "优先待核验", "B": "普通待核验", "C": "人工复核", "D": "过滤留档"}


def screen_intake(title: str, evidence_text: str) -> IntakeScreeningResult:
    """Conservative A/B/C/D screening; uncertain content is never discarded."""
    text = f"{title}\n{evidence_text}".strip()
    for term in _FILTER_TERMS + _SENIOR_TERMS:
        if term in text:
            return IntakeScreeningResult("D", "过滤留档", f"原文出现“{term}”，不适合学生招聘入库。", term, "高")
    for term in _A_TERMS:
        if term in text:
            return IntakeScreeningResult("A", "优先待核验", f"原文出现“{term}”学生或初级岗位信号。", term, "高")
    if "社会招聘" in text or "社会人员" in text:
        return IntakeScreeningResult("B", "普通待核验", "属于社会招聘，需人工确认是否开放给应届生。", "社会招聘", "中")
    return IntakeScreeningResult("C", "人工复核", "原文缺少明确学生适配信号，保留给人工复核。", "未发现直接适配证据", "低")


def build_intake_screening_prompt(title: str, evidence_text: str) -> str:
    return f"""你是学生招聘线索初筛助手。只根据原文判断 A/B/C/D，返回纯 JSON：grade, reason, evidence, confidence。
A=实习、校招、应届或毕业两年内初级岗位；B=可能适配但需要人工确认；C=原文不完整或适配不明；D=体检、面试、录用等进度通知，或明确高职称、负责人、三年以上经验、年龄偏高、收费招聘等。
evidence 必须是原文中连续出现的短语。不得推断岗位条件。grade 只能是 A/B/C/D，confidence 只能是 高/中/低。
标题：{title}\n原文：{evidence_text[:6000]}"""


def _hard_filter(title: str, evidence_text: str) -> IntakeScreeningResult | None:
    text = f"{title}\n{evidence_text}".strip()
    for term in _FILTER_TERMS + _SENIOR_TERMS:
        if term in text:
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
        return IntakeScreeningResult(
            grade,
            _ROUTES[grade],
            reason or "AI 根据原文给出的初筛建议。",
            evidence,
            confidence,
        )
    except Exception:
        return IntakeScreeningResult(
            "C", "人工复核", "AI 初筛不可用或证据无法核验，保守转人工复核。", "AI 初筛未完成", "低"
        )
