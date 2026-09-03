from dataclasses import dataclass
from datetime import date, datetime
import json
from typing import Callable
from urllib.parse import urlparse

from app.models import Job


@dataclass(frozen=True)
class SuggestedScore:
    eligible: bool
    score: int
    status: str
    reason: str
    breakdown: dict[str, int]
    confidence: str


def _is_placeholder(value: str) -> bool:
    return not value.strip() or value.strip() in {
        "待人工判断", "待分类", "待人工分类", "待核验", "原文待人工确认", "以公告原文为准", "地区待定"
    }


def _is_eligible(job: Job) -> bool:
    return (
        not job.is_demo
        and job.status == "待核验"
        and job.notice_type == "新招聘"
        and job.intake_grade in {"A", "B", "C"}
        and job.quality_score == 0
    )


def _source_points(job: Job) -> int:
    host = urlparse(job.source_url).netloc.lower()
    if host.endswith(".gov.cn") or host.endswith(".edu.cn") or "career" in host or "recruit" in host:
        return 20
    if host:
        return 12
    return 0


def _completeness_points(job: Job) -> int:
    points = 0
    points += 4 if not _is_placeholder(job.job_title) else 0
    points += 3 if not _is_placeholder(job.recruitment_type) else 0
    points += 3 if not _is_placeholder(job.location_detail) else 0
    points += 3 if not _is_placeholder(job.target_audience) else 0
    points += 4 if job.official_url.strip() else 0
    points += 3 if not _is_placeholder(job.deadline) else 0
    return points


def _location_points(job: Job) -> int:
    text = f"{job.location_category} {job.location_detail} {job.evidence_text}"
    if "上海" in text:
        return 10
    if "全国" in text:
        return 5
    return 0


def _freshness_points(job: Job, today: date) -> int:
    if not job.collected_at:
        return 2
    age_days = max(0, (today - job.collected_at.date()).days)
    if age_days == 0:
        return 10
    if age_days <= 3:
        return 7
    if age_days <= 7:
        return 4
    return 1


def _actionability_points(job: Job) -> int:
    return 5 if job.source_url.strip() and len(job.evidence_text.strip()) >= 80 else 2


def _baseline(job: Job, today: date) -> dict[str, int]:
    return {
        "来源可信": _source_points(job),
        "信息完整": _completeness_points(job),
        "上海关联": _location_points(job),
        "时效性": _freshness_points(job, today),
        "可执行性": _actionability_points(job),
        "学生适配": 0,
        "岗位价值": 0,
    }


def build_scoring_prompt(job: Job) -> str:
    return f"""你是上海大学生招聘内容运营的评分助手。只根据原文，返回纯 JSON：student_fit_points,value_points,reason,evidence,confidence。
student_fit_points 是0-25；value_points是0-10。学生适配看实习、校招、应届、毕业两年内、专业匹配等明确事实。岗位价值看明确的优质雇主、成长机会或岗位相关性，不得猜测薪资、编制或转正。evidence 必须是原文连续短语，confidence 只能为高/中/低。
标题：{job.job_title}\n原文：{job.evidence_text[:6000]}"""


def _ai_points(job: Job, complete: Callable[[str], str]) -> tuple[int, int, str, str, str]:
    content = complete(build_scoring_prompt(job))
    payload = json.loads(content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    fit = int(payload.get("student_fit_points"))
    value = int(payload.get("value_points"))
    reason = str(payload.get("reason") or "").strip()
    raw_evidence = payload.get("evidence")
    if isinstance(raw_evidence, list):
        evidence_items = [str(item).strip() for item in raw_evidence if str(item).strip()]
        evidence = "；".join(evidence_items)
    else:
        evidence_items = [str(raw_evidence or "").strip()]
        evidence = evidence_items[0]
    confidence = str(payload.get("confidence") or "").strip()
    context = f"{job.job_title}\n{job.evidence_text}"
    if not 0 <= fit <= 25 or not 0 <= value <= 10:
        raise ValueError("AI 建议分超出允许范围")
    if (
        not evidence_items
        or any(item not in context for item in evidence_items)
        or confidence not in {"高", "中", "低"}
    ):
        raise ValueError("AI 评分证据不可核验")
    return fit, value, reason, evidence, confidence


def suggest_job_score(
    job: Job,
    complete: Callable[[str], str] | None = None,
    today: date | None = None,
) -> SuggestedScore:
    if not _is_eligible(job):
        return SuggestedScore(False, 0, "不适用", "该记录不满足建议分批处理条件。", {}, "低")

    breakdown = _baseline(job, today or date.today())
    baseline_score = sum(breakdown.values())
    if complete is None:
        status = "规则建议"
        reason = "模型未配置或未调用，建议分仅基于来源、完整度、地点、时效和可执行性。"
        confidence = "低"
    else:
        try:
            fit, value, reason, evidence, confidence = _ai_points(job, complete)
            breakdown["学生适配"] = fit
            breakdown["岗位价值"] = value
            status = "AI建议"
            reason = f"{reason} 原文依据：{evidence}"
        except Exception:
            status = "规则建议"
            reason = "AI 未返回可核验的评分依据，建议分仅使用确定性规则，需人工复核。"
            confidence = "低"

    score = sum(breakdown.values())
    if job.intake_grade == "C":
        score = min(score, 69)
    return SuggestedScore(True, score, status, reason, breakdown, confidence)
