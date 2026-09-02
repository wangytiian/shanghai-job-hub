from dataclasses import dataclass


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
