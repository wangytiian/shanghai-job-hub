from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.models import Job, Source


@dataclass(frozen=True)
class SeedResult:
    created_jobs: int
    updated_jobs: int


DEMO_SOURCES = (
    ("国家大学生就业服务平台（演示）", "https://www.ncss.cn/", "一级", "公共平台", 4),
    ("上海市国资委招聘栏目（演示）", "https://www.gzw.sh.gov.cn/", "一级", "公共平台", 2),
    ("上海市人力资源和社会保障局（演示）", "https://rsj.sh.gov.cn/", "一级", "公共平台", 4),
    ("中国工商银行人才招聘（演示）", "https://job.icbc.com.cn/", "一级", "企业官网", 4),
    ("中国银行人才招聘（演示）", "https://www.boc.cn/", "一级", "企业官网", 4),
    ("上海农商银行招聘（演示）", "https://www.shrcb.com/", "一级", "企业官网", 6),
    ("德勤中国校园招聘（演示）", "https://www.deloitte.com/cn/", "一级", "企业官网", 6),
    ("中信证券招聘（演示）", "https://careers.citics.com/", "一级", "企业官网", 6),
)


DEMO_JOBS = (
    ("示例金融集团|2027届暑期实习|财务分析|上海|2027届", "示例金融集团", "财务分析实习生", "财务分析", "实习", "明确上海", "上海市浦东新区", "大三实习", "会计审计、金融银行", "2026-09-30", 88, "待审核"),
    ("示例国有银行|2027届校园招聘|管理培训|上海|2027届", "示例国有银行", "管理培训生", "管理培训", "校招", "明确上海", "上海市黄浦区", "大四/应届校招", "金融银行、工商运营", "2026-10-15", 92, "待审核"),
    ("示例会计师事务所|2027届校园招聘|审计|上海|2027届", "示例会计师事务所", "审计助理", "审计", "校招", "明确上海", "上海市静安区", "大四/应届校招", "会计审计、税务评估", "2026-11-01", 90, "待审核"),
    ("示例证券公司|2027届暑期实习|投行|上海|2027届", "示例证券公司", "投行实习生", "投资银行", "实习", "明确上海", "上海市浦东新区", "大三实习", "证券保险、金融银行", "2026-09-20", 85, "待审核"),
    ("示例保险集团|2027届校园招聘|精算|上海|2027届", "示例保险集团", "精算培训生", "精算", "校招", "明确上海", "上海市徐汇区", "大四/应届校招", "证券保险、数据技术", "2026-10-08", 86, "待审核"),
    ("示例上海国企|2026届初级招聘|合规|上海|毕业两年内", "示例上海国企", "合规专员", "合规", "初级社招", "明确上海", "上海市杨浦区", "毕业两年内初级岗位", "法律合规、公共管理", "2026-09-18", 82, "待审核"),
    ("示例跨国企业|2027届校园招聘|供应链|上海|2027届", "示例跨国企业", "供应链管理培训生", "供应链", "校招", "明确上海", "上海市长宁区", "大四/应届校招", "工商运营、国际商务", "2026-10-22", 80, "待审核"),
    ("示例科技企业|2027届暑期实习|数据分析|上海|2027届", "示例科技企业", "数据分析实习生", "数据分析", "实习", "明确上海", "上海市闵行区", "大三实习", "数据技术、经济统计", "2026-09-25", 84, "待审核"),
    ("示例公共服务机构|2027届校园招聘|综合管理|上海|2027届", "示例公共服务机构", "综合管理岗", "综合管理", "校招", "明确上海", "上海市普陀区", "大四/应届校招", "公共管理、语言及综合职能", "2026-10-30", 78, "待审核"),
    ("示例国际贸易公司|2026届初级招聘|商务运营|上海|毕业两年内", "示例国际贸易公司", "商务运营专员", "商务运营", "初级社招", "明确上海", "上海市虹口区", "毕业两年内初级岗位", "国际商务、工商运营", "2026-09-28", 76, "待审核"),
)


def seed_demo_data(session) -> SeedResult:
    for name, url, level, source_type, frequency in DEMO_SOURCES:
        source = session.scalar(select(Source).where(Source.name == name))
        if source is None:
            session.add(
                Source(
                    name=name,
                    url=url,
                    level=level,
                    source_type=source_type,
                    check_frequency_hours=frequency,
                )
            )

    created_jobs = 0
    updated_jobs = 0
    source_url = DEMO_SOURCES[0][1]
    for record in DEMO_JOBS:
        (
            fingerprint,
            employer_name,
            job_title,
            job_family,
            recruitment_type,
            location_category,
            location_detail,
            target_audience,
            direction_tags,
            deadline,
            quality_score,
            status,
        ) = record
        job = session.scalar(select(Job).where(Job.fingerprint == fingerprint))
        if job is None:
            session.add(
                Job(
                    fingerprint=fingerprint,
                    employer_name=employer_name,
                    job_title=job_title,
                    job_family=job_family,
                    recruitment_type=recruitment_type,
                    location_category=location_category,
                    location_detail=location_detail,
                    target_audience=target_audience,
                    direction_tags=direction_tags,
                    deadline=deadline,
                    official_url="https://example.com/demo-official-job",
                    source_url=source_url,
                    evidence_text="演示原文证据：该岗位仅用于验证本地招聘内容审核流程，不代表真实招聘。",
                    quality_score=quality_score,
                    risk_flags="演示数据，不代表真实招聘",
                    is_demo=True,
                    status=status,
                    intake_grade="A",
                    intake_route="优先待核验",
                    intake_reason="演示岗位：实习、校招或毕业两年内初级岗位",
                    intake_evidence="演示数据",
                    intake_confidence="高",
                )
            )
            created_jobs += 1
        else:
            if job.intake_grade not in {"A", "B"}:
                job.intake_grade = "A"
                job.intake_route = "优先待核验"
                job.intake_reason = "演示岗位：实习、校招或毕业两年内初级岗位"
                job.intake_evidence = "演示数据"
                job.intake_confidence = "高"
            job.version += 1
            job.updated_at = datetime.now()
            updated_jobs += 1

    session.commit()
    return SeedResult(created_jobs=created_jobs, updated_jobs=updated_jobs)
