from dataclasses import dataclass
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DistributionItem, Job
from app.services.jobs import UNSPECIFIED_DEADLINE


NON_OFFICIAL_NOTICE = "本内容为面向上海立信会计金融学院学生的非官方就业信息服务；请以官方原文为准。"
PLACEHOLDER_VALUES = {"以公告原文为准", "待人工判断", "待分类", "待核验", "原文待人工确认", "原文未明确"}


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


def _application_copy(job: Job) -> tuple[str, str]:
    official_url = escape(job.official_url, quote=True)
    deadline_unstated = job.deadline.strip() == UNSPECIFIED_DEADLINE
    deadline_instruction = (
        "公告未明确统一截止时间，建议尽快查看官方原文或附件确认报名安排。"
        if deadline_unstated
        else "请在截止日期前"
    )
    if job.application_method == "email":
        contact = escape(job.application_contact)
        return (
            f'<p style="margin:0 0 10px;">{deadline_instruction}，按招聘单位公告要求准备材料。请将材料发送至：<strong>{contact}</strong>。</p>'
            f'<p style="margin:0;color:#667085;font-size:12px;word-break:break-all;">招聘单位官方公告：{official_url}</p>',
            f"投递方式：邮件投递\n报名邮箱：{job.application_contact}\n招聘单位官方公告：{job.official_url}",
        )
    if deadline_unstated:
        instruction = "公告未明确统一截止时间，建议尽快查看官方原文或附件确认报名安排。"
    elif job.application_method == "official_platform":
        instruction = "请在截止日期前进入招聘单位官方招聘平台完成申请。"
    elif job.application_method == "on_site":
        instruction = "请按招聘单位官方公告的时间、地点和材料要求现场报名。"
    else:
        instruction = "请在截止日期前通过招聘单位官方公告所示入口完成申请。"
    return (
        f'<p style="margin:0 0 10px;">{instruction}</p>'
        f'<p style="margin:14px 0;text-align:center;"><a href="{official_url}" style="display:inline-block;padding:10px 20px;border-radius:5px;background:#0B4A87;color:#FFFFFF;text-decoration:none;font-weight:700;">查看招聘单位官方公告</a></p>'
        f'<p style="margin:0;color:#667085;font-size:12px;word-break:break-all;">招聘单位官方公告：{official_url}</p>',
        f"投递方式：{instruction}\n招聘单位官方公告：{job.official_url}",
    )


def build_wechat_draft(job: Job) -> WechatDraft:
    """Render a compact, clipboard-ready article using only verified job fields."""
    employer = escape(job.employer_name)
    job_title = escape(job.job_title)
    source_url = escape(job.source_url, quote=True)
    is_summary = job.posting_scope == "multi_role_announcement"
    heading = "公告速览" if is_summary else "招聘速览"
    introduction = (
        f"{employer}发布了{job_title}。该公告包含多个岗位，请按已核验附件或官方公告查看具体岗位、条件与投递要求。"
        if is_summary
        else f"{employer}正在招聘{job_title}。以下内容仅整理已核验的招聘事实，投递前请再次查看招聘单位官方公告。"
    )
    facts = "".join(
        (_line("招聘单位", job.employer_name), _line("公告/岗位名称", job.job_title),
         _line("招聘类型", job.recruitment_type), _line("工作地点", job.location_detail),
         _line("适合人群", job.target_audience), _line("专业方向", job.direction_tags),
         _line("申请截止", job.deadline))
    )
    application_html, application_text = _application_copy(job)
    draft_title = f"{job.employer_name}｜{job.job_title}"
    digest_parts = [job.recruitment_type]
    if _known(job.location_detail):
        digest_parts.append(job.location_detail)
    if _known(job.deadline):
        digest_parts.append(f"截止 {job.deadline}")
    digest = "｜".join(digest_parts)
    html = f'''<section style="max-width:677px;margin:0 auto;padding:8px 18px 28px;color:#17213A;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;font-size:16px;line-height:1.85;box-sizing:border-box;">
  <h1 style="margin:0 0 8px;color:#102A43;font-size:29px;line-height:1.35;font-weight:700;letter-spacing:0.02em;">{employer}｜{job_title}</h1>
  <p style="margin:0 0 20px;color:#667085;font-size:14px;">沪上求职汇　｜　上海　｜　人工核验后生成</p>
  <p style="margin:0 0 24px;">{introduction}</p>
  <div style="height:1px;margin:28px 0;background:#D9E2EC;"></div>
  <h2 style="margin:2px 0 18px;text-align:center;color:#102A43;font-size:23px;line-height:1.35;">{heading}</h2>
  {facts}
  <div style="height:1px;margin:28px 0;background:#D9E2EC;"></div>
  <h2 style="margin:2px 0 16px;text-align:center;color:#102A43;font-size:23px;line-height:1.35;">如何投递</h2>
  {application_html}
  <div style="height:1px;margin:28px 0 14px;background:#D9E2EC;"></div>
  <p style="margin:5px 0;color:#667085;font-size:12px;">信息来源｜<a href="{source_url}" style="color:#667085;">招聘单位公开原文</a></p>
  <p style="margin:5px 0;color:#667085;font-size:12px;">核验说明｜本稿由人工对照公开原文核验后生成；请以官方公告为准。</p>
  <div style="height:1px;margin:18px 0;background:#D9E2EC;"></div>
  <p style="margin:0;text-align:center;color:#667085;font-size:12px;">面向青年求职者的非官方就业信息服务</p>
</section>'''
    plain_lines = [draft_title, "", f"招聘类型：{job.recruitment_type}"]
    for label, value in (("地点", job.location_detail), ("适合人群", job.target_audience), ("专业方向", job.direction_tags), ("截止日期", job.deadline)):
        if _known(value):
            plain_lines.append(f"{label}：{value}")
    plain_lines.extend(("", application_text, "", NON_OFFICIAL_NOTICE))
    return WechatDraft(title=draft_title, digest=digest, html=html, plain_text="\n".join(plain_lines))


def _public_article(job: Job) -> str:
    return build_wechat_draft(job).plain_text


def _group_message(job: Job) -> str:
    location = f"｜地点：{job.location_detail}" if _known(job.location_detail) else ""
    deadline = (
        "公告未明确统一截止时间，建议尽快查看官方原文或附件确认报名安排"
        if job.deadline.strip() == UNSPECIFIED_DEADLINE
        else job.deadline
    )
    return (f"【{job.recruitment_type}】{job.employer_name} {job.job_title}\n"
            f"适合：{job.target_audience}｜{job.direction_tags}{location}\n"
            f"报名时间：{deadline}\n招聘单位官方公告：{job.official_url}\n{NON_OFFICIAL_NOTICE}")


def create_distribution_items(session: Session, job_id: int) -> list[DistributionItem]:
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError("岗位不存在")
    if job.status != "可发布":
        raise ValueError("只有可发布岗位可以生成分发内容")
    if job.intake_grade not in {"A", "B"}:
        raise ValueError("该岗位入库分级为 C/D，只有 A/B 级岗位可以进入学生渠道分发")
    if job.distribution_recommendation == "不进入学生分发":
        raise ValueError("该岗位不适合核心学生用户，已保留资料库，不生成学生渠道内容")
    wanted = (("公众号", "公众号草稿", _public_article(job)), ("微信群", job.target_audience, _group_message(job)))
    items: list[DistributionItem] = []
    for channel, audience_group, content in wanted:
        item = session.scalar(select(DistributionItem).where(DistributionItem.job_id == job.id, DistributionItem.channel == channel))
        if item is None:
            item = DistributionItem(job_id=job.id, channel=channel, audience_group=audience_group, content=content)
            session.add(item)
        else:
            item.audience_group, item.content = audience_group, content
        items.append(item)
    session.commit()
    return items
