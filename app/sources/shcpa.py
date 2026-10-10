"""Read only the association's public, listed recruitment announcements."""

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from app.services.source_request_budget import RequestBudgetController
from app.sources.campus_json import CampusJobDetail
from app.sources.shanghai_sasac import USER_AGENT


SHCPA_LIST_URL = 'https://www.shcpa.org.cn/recruit/getRecruitQueryList.do'
_DETAIL_PATH = '/recruit/getRecruitDetailedView.do'


@dataclass(frozen=True)
class ShcpaListing:
    parent_id: str
    title: str
    published_at: str
    detail_url: str
    effective_date: str = ''


def _detail_url(value: str) -> tuple[str, str]:
    url = urlsplit(urljoin(SHCPA_LIST_URL, value))
    try:
        valid = url.scheme == 'https' and url.hostname == 'www.shcpa.org.cn' and url.port in (None, 443)
    except ValueError:
        valid = False
    query = parse_qs(url.query)
    rid = query.get('rid', [''])[0]
    if not valid or url.username or url.password or url.path != _DETAIL_PATH or set(query) != {'rid'} or len(query['rid']) != 1 or not re.fullmatch(r'\d+', rid):
        raise ValueError('注协详情仅允许公开列表中的官方 rid 直链')
    return f'https://www.shcpa.org.cn{_DETAIL_PATH}?rid={rid}', rid


def parse_shcpa_listings(html: str) -> list[ShcpaListing]:
    soup = BeautifulSoup(html, 'html.parser')
    result, seen = [], set()
    for table in soup.find_all('table'):
        if '事务所名称' not in table.get_text() or '有效日期' not in table.get_text():
            continue
        for row in table.find_all('tr'):
            cells = row.find_all('td', recursive=False)
            if len(cells) != 4:
                continue
            link = cells[1].find('a', href=True)
            if not link:
                continue
            try:
                url, rid = _detail_url(link['href'])
            except ValueError:
                continue
            if rid in seen:
                continue
            title = link.get_text(' ', strip=True)
            if not title:
                continue
            result.append(ShcpaListing(rid, title, cells[3].get_text(' ', strip=True), url, cells[2].get_text(' ', strip=True)))
            seen.add(rid)
    return result


def parse_shcpa_detail(html: str, listing: ShcpaListing) -> CampusJobDetail:
    url, rid = _detail_url(listing.detail_url)
    if rid != listing.parent_id:
        raise ValueError('注协 rid 身份不匹配')
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'iframe', 'template']):
        tag.decompose()
    fields = {}
    for table in soup.find_all('table'):
        if '职位名称' not in table.get_text():
            continue
        for row in table.find_all('tr'):
            cells = row.find_all('td', recursive=False)
            if len(cells) == 2:
                key = cells[0].get_text(' ', strip=True).rstrip('：:').strip()
                fields[key] = cells[1].get_text(' ', strip=True)
    if not fields.get('职位名称') or not fields.get('职位要求'):
        raise ValueError('注协详情缺少岗位名称或职位要求正文')
    lines = [f'招聘单位：{listing.title}'] + [f'{key}：{value}' for key, value in fields.items() if value]
    lines.append(f"任职要求：{fields['职位要求']}")
    # This is the recruitment-specific contact field, never a company-intro email.
    emails = re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', fields.get('联系方式', ''))
    if len(set(emails)) == 1:
        lines.append(f'官方投递邮箱：{emails[0]}')
    deadline = fields.get('有效日期', '')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', deadline):
        lines.append(f'报名截止日期：{deadline}')
    for node in soup.find_all(['p', 'div']):
        if node.find(['p', 'div']):
            continue
        text = node.get_text(' ', strip=True)
        if '自行上传招聘信息' in text or '真实性、合法性' in text:
            lines.append(f'协会声明：{text}')
    # Neither association footer nor company name is a workplace fact.
    body = '\n'.join(fields.get(k, '') for k in ('职位要求', '事务所简介', '联系方式'))
    places = re.findall(r'(?:工作地点|工作地址|岗位地点)\s*[:：]\s*([^。；;\n]+)', body)
    location = places[0].strip() if len(places) == 1 else ''
    category = '上海' if location.startswith('上海') else ('外地' if location else '待核验')
    return CampusJobDetail(fields['职位名称'], fields.get('发布日期', ''), url, '\n'.join(lines), rid, listing.title, listing.title, category, location, '')


def _request(client, url: str, *, timeout: float, controller: RequestBudgetController, data=None):
    bounded_timeout = min(12.0, timeout, controller.before_request())
    kwargs = dict(timeout=bounded_timeout, headers={'User-Agent': USER_AGENT}, follow_redirects=False)
    response = client.get(url, **kwargs) if data is None else client.post(url, data=data, **kwargs)
    controller.after_request()
    response.raise_for_status()
    return response.text


def _budget(timeout):
    return RequestBudgetController(total_seconds=600, request_timeout_seconds=min(timeout, 12), interval_seconds=1)


def fetch_shcpa_listings(client, pages=1, *, timeout=12.0, controller=None):
    if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= 3:
        raise ValueError('注协试采页数必须为1至3')
    controller = controller or _budget(timeout)
    html = _request(client, SHCPA_LIST_URL, timeout=timeout, controller=controller)
    result = parse_shcpa_listings(html)[:30]
    soup = BeautifulSoup(html, 'html.parser')
    form = soup.find('form', attrs={'name': 'formrecruitquery'})
    if form and form.get('method', '').lower() == 'post' and form.find('input', attrs={'name': 'currentPage'}):
        observed = sorted({int(m.group(1)) for a in form.find_all('a', onclick=True) if (m := re.fullmatch(r"(?:javascript:)?doSearchByPage\('([0-9]+)'\);?", a['onclick']))})
        seen = {item.parent_id for item in result}
        for page in observed[:max(0, min(pages, 3) - 1)]:
            if len(result) >= 30:
                break
            more = _request(client, SHCPA_LIST_URL, timeout=timeout, controller=controller, data={'currentPage': str(page), 'txtofficename': ''})
            for item in parse_shcpa_listings(more):
                if item.parent_id not in seen and len(result) < 30:
                    result.append(item)
                    seen.add(item.parent_id)
    return result


def fetch_shcpa_detail(client, listing, *, timeout=12.0, controller=None):
    url, rid = _detail_url(listing.detail_url)
    if rid != listing.parent_id:
        raise ValueError('注协 rid 身份不匹配')
    controller = controller or _budget(timeout)
    return parse_shcpa_detail(_request(client, url, timeout=timeout, controller=controller), listing)
