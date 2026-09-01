from dataclasses import dataclass


FIT_LEVELS = {"核心适配", "补充适配", "不适合核心学生用户", "待人工判断"}
DISTRIBUTION_RECOMMENDATIONS = {"进入学生分发审核", "仅保留资料库", "不进入学生分发"}

_BLOCKING_TERMS = ("副教授", "正教授", "博士后", "高级职称", "高级专业技术", "3年以上", "三年以上", "负责人", "总经理", "行长", "总监")
_CORE_TERMS = ("实习", "应届", "校园招聘", "校招", "管培生", "毕业两年")
_SUPPLEMENTARY_TERMS = ("社会人员", "工作经验", "硕士", "博士", "中级职称")


@dataclass(frozen=True)
class StudentFitRecommendation:
    student_fit_level: str
    distribution_recommendation: str
    rationale: str
    confidence: str


def _matches(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in text]


def recommend_student_fit(
    evidence_text: str,
    target_audience: str = "",
    suggested_level: str = "",
    suggested_distribution: str = "",
    suggested_rationale: str = "",
    suggested_confidence: str = "",
) -> StudentFitRecommendation:
    """Return a conservative student-routing recommendation with hard-rule overrides."""
    evidence = f"{evidence_text}\n{target_audience}".strip()
    blocking_matches = _matches(evidence, _BLOCKING_TERMS)
    if blocking_matches:
        labels = "、".join(blocking_matches[:3])
        return StudentFitRecommendation(
            "不适合核心学生用户",
            "不进入学生分发",
            f"原文出现 {labels} 等要求，与大三、大四和毕业两年内核心用户不匹配。",
            "高",
        )

    core_matches = _matches(evidence, _CORE_TERMS)
    if core_matches:
        labels = "、".join(core_matches[:3])
        return StudentFitRecommendation(
            "核心适配",
            "进入学生分发审核",
            f"原文出现 {labels} 等学生或初级岗位信号。",
            "高",
        )

    if suggested_level in FIT_LEVELS and suggested_distribution in DISTRIBUTION_RECOMMENDATIONS:
        return StudentFitRecommendation(
            suggested_level,
            suggested_distribution,
            suggested_rationale.strip() or "AI 根据公告全文给出的运营判断，需人工确认。",
            suggested_confidence if suggested_confidence in {"高", "中", "低"} else "中",
        )

    supplementary_matches = _matches(evidence, _SUPPLEMENTARY_TERMS)
    if supplementary_matches:
        labels = "、".join(supplementary_matches[:3])
        return StudentFitRecommendation(
            "补充适配",
            "仅保留资料库",
            f"原文主要面向 {labels} 等非核心学生人群，建议保留但不默认分发。",
            "中",
        )

    return StudentFitRecommendation(
        "待人工判断",
        "仅保留资料库",
        "原文未出现足以确认核心学生适配度的直接条件。",
        "低",
    )
