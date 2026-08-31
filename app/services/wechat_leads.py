from __future__ import annotations

import hashlib
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, Source
from app.services.notice_classification import suggest_notice_type


PROMOTION_KEYWORDS = ("课程", "培训", "报班", "优惠", "试听", "开课")
RECRUITMENT_KEYWORDS = ("招聘", "招录", "报名", "事业单位", "公务员", "国企", "社工", "辅警")


class _WechatArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._content_depth = 0
        self.content_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") in {"og:title", "twitter:title"}:
            self.title = attributes.get("content") or self.title
        if tag == "title":
            self._in_title = True
        if attributes.get("id") == "js_content":
            self._content_depth = 1
        elif self._content_depth:
            self._content_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if self._content_depth:
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data
        if self._content_depth and data.strip():
            self.content_parts.append(data.strip())


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _validate_public_wechat_url(url: str) -> str:
    normalized = url.strip()
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or parsed.netloc.lower() != "mp.weixin.qq.com" or not parsed.path:
        raise ValueError("请提供微信公众号公开文章链接（https://mp.weixin.qq.com/...）")
    return normalized


def _extract_article(html: str) -> tuple[str, str]:
    parser = _WechatArticleParser()
    parser.feed(html)
    title = _normalize_text(parser.title)
    evidence_text = _normalize_text(" ".join(parser.content_parts))
    if not title or len(evidence_text) < 30:
        raise ValueError("未能读取公众号文章的有效标题或正文，请确认链接可公开访问")
    return title, evidence_text


def _is_promotion_only(title: str, evidence_text: str) -> bool:
    content = f"{title} {evidence_text}"
    return any(word in content for word in PROMOTION_KEYWORDS) and not any(
        word in content for word in RECRUITMENT_KEYWORDS
    )


def import_public_wechat_article(session: Session, source: Source, url: str, client) -> Job:
    if source.adapter_key != "wechat_article_lead":
        raise ValueError("该来源不是公众号招聘线索源")
    normalized_url = _validate_public_wechat_url(url)
    existing = session.scalar(select(Job).where(Job.source_url == normalized_url).order_by(Job.id.desc()))
    if existing is not None:
        return existing

    response = client.get(
        normalized_url,
        headers={"User-Agent": "RecruitmentLeadVerifier/1.0 (+local-use)"},
        timeout=12,
    )
    response.raise_for_status()
    title, evidence_text = _extract_article(response.text)
    if _is_promotion_only(title, evidence_text):
        raise ValueError("该文章为课程或培训推广内容，未作为招聘线索入库")

    url_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    content_hash = hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()
    suggestion = suggest_notice_type(title, evidence_text)
    job = Job(
        fingerprint=f"公众号线索|{url_hash}",
        employer_name="待官方核验",
        job_title=title[:160],
        job_family="待人工分类",
        recruitment_type="待核验",
        location_category="地区待定",
        location_detail="以官方原文为准",
        target_audience="待人工判断",
        direction_tags="待人工分类",
        deadline="待官方确认",
        official_url="",
        source_url=normalized_url,
        evidence_text=evidence_text,
        quality_score=60,
        risk_flags="公众号公开文章线索，须补充官方原文和报名入口；未经人工核验不得对外发布",
        is_demo=False,
        collected_at=datetime.now(),
        content_fingerprint=content_hash,
        lifecycle_status="正常",
        status="待核验",
        notice_type="待判断",
        notice_type_suggestion=suggestion,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job
