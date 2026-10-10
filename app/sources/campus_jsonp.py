"""Read only the two verified public campus JSONP endpoints; never evaluate JS."""

from dataclasses import dataclass
import hashlib
import json
import re

from bs4 import BeautifulSoup

from app.sources.campus_json import CampusAnnouncement, CampusJobDetail
from app.services.source_candidate_policy import MULTI_POSITION_PARENT_MARKER
from app.services.source_request_budget import RequestBudgetController


CALLBACK = 'researchCallback'
_WID = re.compile(r'^[a-fA-F0-9]{32}$')
_EMAIL = re.compile(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


@dataclass(frozen=True)
class CampusJsonpSource:
    name: str
    base_url: str
    detail_page_path: str

    @property
    def endpoint(self):
        return self.base_url + '/pros_jiuye/s/zxh/owebsiteData/position'

    @property
    def listing_url(self):
        return self.base_url + self.detail_page_path.split('?')[0]

    def detail_page_url(self, wid):
        return self.base_url + self.detail_page_path.format(wid=wid)


SUES_INTERNSHIP_SOURCE = CampusJsonpSource('上海工程技术大学就业网实习', 'https://career.sues.edu.cn', '/page/positionsx.html?wid={wid}')
SIT_INTERNSHIP_SOURCE = CampusJsonpSource('上海应用技术大学就业网实习', 'https://job.sit.edu.cn', '/page/positionzp.html?wid={wid}&type=1')


def decode_jsonp(value: str) -> dict:
    match = re.fullmatch(r'\s*researchCallback\s*\((.*)\)\s*;?\s*', value, re.S)
    if not match:
        raise ValueError('公开 JSONP 必须使用固定回调且不得包含尾随代码')
    def invalid_constant(value):
        raise ValueError('JSONP 包含非 JSON 常量')
    payload = json.loads(match.group(1), parse_constant=invalid_constant)
    if not isinstance(payload, dict):
        raise ValueError('公开 JSONP 缺少对象')
    return payload


def _rows(value):
    rows = decode_jsonp(value).get('listData')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('公开 JSONP listData 结构异常')
    return rows


def _text(value):
    soup = BeautifulSoup(str(value or ''), 'html.parser')
    for node in soup(['script', 'style']):
        node.decompose()
    for node in soup.find_all('br'):
        node.replace_with('\n')
    for node in soup.find_all(['p', 'div', 'li']):
        node.append('\n')
    return '\n'.join(line.strip() for line in soup.get_text().splitlines() if line.strip())


def parse_jsonp_listings(value, source):
    results, seen = [], set()
    for row in _rows(value)[:10]:
        wid = str(row.get('wid') or '')
        title = _text(row.get('zwmc'))
        if not _WID.fullmatch(wid) or not title or wid in seen:
            continue
        results.append(CampusAnnouncement(wid, title, str(row.get('sxsj') or '')[:10], source.detail_page_url(wid)))
        seen.add(wid)
    return results


def _role_body(body, titles, index):
    # Only a unique, ordered exact title heading establishes a role boundary.
    starts = []
    for title in titles:
        matches = list(re.finditer(r'(?:^|\n)' + re.escape(title) + r'\s*(?:\n|$)', body))
        if len(matches) != 1:
            return ''
        starts.append(matches[0].start())
    if starts != sorted(starts):
        return ''
    return body[starts[index]:starts[index + 1] if index + 1 < len(starts) else len(body)].strip()


def parse_jsonp_details(value, source, listing):
    rows = _rows(value)
    if len(rows) != 1 or str(rows[0].get('wid') or '') != listing.parent_id:
        raise ValueError('详情 wid 与请求不一致或详情记录不唯一')
    row = rows[0]
    body = _text(row.get('zwms'))
    if not body:
        raise ValueError('详情缺少岗位正文')
    positions = row.get('subOpList') or []
    if not isinstance(positions, list) or any(not isinstance(item, dict) for item in positions):
        raise ValueError('subOpList 结构异常')
    titles = [_text(item.get('content')) for item in positions]
    if any(not title for title in titles) or len(set(titles)) != len(titles):
        raise ValueError('岗位标题缺失或重复')
    aggregate = len(titles) == 1 and bool(re.search(r'等(?:岗位|职位)?$|多个岗位|多岗', titles[0]))
    multiple = len(positions) > 1 or aggregate
    result = []
    for index, position in enumerate(positions or [{}]):
        title = titles[index] if titles else listing.title
        scoped = '' if aggregate else (_role_body(body, titles, index) if multiple else body)
        location = _text(position.get('workLocation') or row.get('gzd'))
        if not location:
            match = re.search(r'(?:工作地点|工作地址|岗位地点)[：:]\s*([^。；\n]+)', scoped)
            location = match.group(1).strip() if match else ''
        category = '明确上海' if location.startswith('上海') else ('其他地区' if location else '原文未明确')
        # jlzyyx and jltdfs are explicit application fields; generic company
        # contact fields dwemail/gszy are not assumed to be application evidence.
        email = _EMAIL.search(_text(position.get('applicationEmail') or row.get('jlzyyx') or row.get('jltdfs')))
        if not email:
            match = re.search(r'(?:投递邮箱|简历.{0,8}邮箱|应聘邮箱)[：:]\s*(' + _EMAIL.pattern + ')', scoped)
            email = match
        url = _text(position.get('officialApplyURL') or row.get('officialApplyURL'))
        if not url:
            match = re.search(r'(?:网申地址|网申链接|官方投递入口|简历投递入口|报名链接)[：:]\s*(https://[^\s<>]+)', scoped)
            url = match.group(1).rstrip('。；，;,') if match else ''
        if not re.fullmatch(r'https://[^\s]+', url) or url == listing.detail_url:
            url = ''
        lines = [f'岗位名称：{title}', f'岗位描述：{scoped.replace(chr(10), " ")}', f'岗位地点：{location}']
        if multiple and scoped and not aggregate and row.get('xlyq'):
            # xlyq is a documented platform-wide accepted degree set, unlike
            # qualification prose belonging to a different role in the body.
            lines[1] += f' 任职要求：学历要求：{_text(row["xlyq"])}。'
        # Live schemas establish field4 as salary and field5 as duration;
        # undocumented sub-fields never become eligibility or deadlines.
        if email:
            lines.append(f'官方投递邮箱：{email.group(1) if email.lastindex else email.group(0)}')
        if url:
            lines.append(f'官方报名入口：{url}')
        if multiple:
            lines.append(MULTI_POSITION_PARENT_MARKER)
            if row.get('xlyq'):
                lines.append(f'平台共同学历字段（人工核对）：{_text(row["xlyq"])}')
        lines.append(f'原始正文：{body}')
        if not multiple and row.get('xlyq'):
            lines.append(f'学历要求：{_text(row["xlyq"])}')
        if row.get('jzrq'):
            lines.append(f'申请截止日期：{_text(row["jzrq"])}')
        role_id = hashlib.sha256(title.encode('utf-8')).hexdigest()[:16] if titles else 'position'
        result.append(CampusJobDetail(title=title, published_at=listing.published_at, detail_url=listing.detail_url,
            evidence_text='\n'.join(lines), identity_key=f'{listing.parent_id}:{role_id}', announcement_title=listing.title,
            employer_name=_text(row.get('yrdwmc') or row.get('dwmc')), location_category=category,
            location_detail=location, official_url=url))
    return result


def _controller(controller, timeout):
    if timeout <= 0:
        raise ValueError('请求超时必须为正数')
    return controller or RequestBudgetController(total_seconds=600, request_timeout_seconds=min(timeout, 12), interval_seconds=1)


def _get(client, source, params, timeout, controller):
    if source not in (SUES_INTERNSHIP_SOURCE, SIT_INTERNSHIP_SOURCE):
        raise ValueError('仅允许已核实两校公开端点')
    request_timeout = min(12., timeout, controller.before_request())
    response = client.get(source.endpoint, params={**params, 'callback': CALLBACK}, timeout=request_timeout, follow_redirects=False)
    response.raise_for_status()
    controller.after_request()
    return response.text


def fetch_jsonp_listings(client, source, pages=1, *, timeout=12., controller=None):
    if not 1 <= pages <= 3:
        raise ValueError('采集页数必须在1到3页之间')
    controller = _controller(controller, timeout)
    result, seen = [], set()
    for page in range(1, pages + 1):
        entries = parse_jsonp_listings(_get(client, source, {'fbbk': 1, 'eachPageRows': 10, 'currentPageno': page, 'type': 'list'}, timeout, controller), source)
        new = [item for item in entries if item.parent_id not in seen]
        result.extend(new)
        seen.update(item.parent_id for item in new)
        if not new:
            break
    return result[:30]


def fetch_jsonp_details(client, source, listing, *, timeout=12., controller=None):
    if not _WID.fullmatch(listing.parent_id) or listing.detail_url != source.detail_page_url(listing.parent_id):
        raise ValueError('只允许来自固定官方单岗位直链的 wid')
    controller = _controller(controller, timeout)
    return parse_jsonp_details(_get(client, source, {'wid': listing.parent_id, 'type': 'input'}, timeout, controller), source, listing)
