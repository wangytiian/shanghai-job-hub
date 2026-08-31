from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Source
from app.sources.shanghai_sasac import LISTING_URL as SHANGHAI_SASAC_URL


@dataclass(frozen=True)
class OfficialSourceDefinition:
    name: str
    url: str
    level: str
    source_type: str
    adapter_key: str
    scope_group: str
    is_enabled: bool
    library_tier: str
    student_value_score: int
    adaptation_status: str
    next_action: str


_TIER_DEFAULTS = {
    "A": (True, "已自动采集", "保持定时采集并人工核验"),
    "B": (False, "待专用适配", "完成公开页面试跑与正文清洗"),
    "C": (False, "重点监控", "检查官网招聘页是否更新"),
    "D": (False, "观察中", "在招聘季进行官方入口复查"),
}


def _source(name: str, url: str, scope_group: str, tier: str, score: int, *, adapter_key: str = "pending_validation", source_type: str = "企业官网") -> OfficialSourceDefinition:
    is_enabled, adaptation_status, next_action = _TIER_DEFAULTS[tier]
    return OfficialSourceDefinition(name, url, "一级", source_type, adapter_key, scope_group, is_enabled, tier, score, adaptation_status, next_action)


OFFICIAL_SOURCE_CATALOG = (
    # A: verified, public collectors
    _source("上海市国资委国企招聘（真实公开来源）", SHANGHAI_SASAC_URL, "上海国企", "A", 95, adapter_key="shanghai_sasac", source_type="政府公开栏目"),
    _source("上海市人社局事业单位公开招聘", "https://rsj.sh.gov.cn/tsydwgkzp_17406/index.html", "上海事业单位", "A", 92, adapter_key="official_dated_list", source_type="政府公开栏目"),
    _source("国务院国资委人事招聘", "https://www.sasac.gov.cn/n2588035/n2588325/n2588350/index.html", "央企全国", "A", 90, adapter_key="official_dated_list", source_type="政府公开栏目"),
    _source("上海市税务局公务员招录", "https://shanghai.chinatax.gov.cn/xxgk/rsxx/gwyzl/", "上海公共部门", "A", 90, adapter_key="official_dated_list", source_type="政府公开栏目"),
    # B: core sites awaiting a dedicated public-page adapter
    _source("上海银行官方招聘（待专用适配）", "https://hr.bosc.cn/", "上海金融", "B", 88),
    _source("上海浦东发展银行官方招聘", "https://job.spdb.com.cn/", "上海金融", "A", 88, adapter_key="spdb_shanghai_jobs"),
    _source("上海农村商业银行官方招聘（待专用适配）", "https://job.srcb.com/", "上海金融", "B", 86),
    _source("国泰海通证券官方招聘（待专用适配）", "https://zhaopin.gtht.com/", "上海证券", "B", 85),
    _source("东方证券官方招聘（待专用适配）", "https://job.dfzq.com.cn/", "上海证券", "B", 82),
    _source("中国太保官方招聘（待专用适配）", "https://job.cpic.com.cn/", "上海保险", "B", 82),
    _source("上海国际集团官方招聘（待专用适配）", "https://www.sigchina.com/", "上海国企", "B", 78),
    _source("上海电气官方招聘（待专用适配）", "https://www.shanghai-electric.com/group/job/", "上海国企", "B", 80),
    _source("上汽集团官方招聘（待专用适配）", "https://www.saicmotor.com/chinese/careers/", "上海国企", "B", 80),
    _source("上海城投官方招聘（待专用适配）", "https://www.shanghai-chengtou.com/", "上海国企", "B", 76),
    _source("上海建工官方招聘（待专用适配）", "https://www.scg.com.cn/", "上海国企", "B", 76),
    _source("申能集团官方招聘（待专用适配）", "https://www.shen-neng.com/", "上海国企", "B", 76),
    _source("上海机场集团官方招聘（待专用适配）", "https://www.shanghaiairport.com/", "上海国企", "B", 76),
    _source("上港集团官方招聘（待专用适配）", "https://www.portshanghai.com.cn/", "上海国企", "B", 74),
    _source("锦江国际官方招聘（待专用适配）", "https://www.jinjiang.com/", "上海国企", "B", 74),
    _source("光明食品集团官方招聘（待专用适配）", "https://www.brightfood.com/", "上海国企", "B", 74),
    _source("建设银行官方招聘（待专用适配）", "https://www1.ccb.com/cn/recruit/index.html", "银行", "B", 82),
    _source("工商银行官方招聘（待专用适配）", "https://job.icbc.com.cn/", "银行", "B", 82),
    _source("农业银行官方招聘（待专用适配）", "https://career.abchina.com/", "银行", "B", 80),
    _source("中国银行官方招聘（待专用适配）", "https://www.boc.cn/aboutboc/ab8/", "银行", "B", 80),
    _source("交通银行官方招聘（待专用适配）", "https://job.bankcomm.com/", "银行", "B", 84),
    _source("招商银行官方招聘（待专用适配）", "https://career.cmbchina.com/social/home", "银行", "B", 82),
    _source("中信银行官方招聘（待专用适配）", "https://job.citicbank.com/", "银行", "B", 82),
    _source("德勤中国官方招聘（待专用适配）", "https://www.deloitte.com/cn/zh/careers.html", "专业服务", "B", 80),
    _source("普华永道中国官方招聘（待专用适配）", "https://www.pwccn.com/zh/careers.html", "专业服务", "B", 80),
    _source("安永中国官方招聘（待专用适配）", "https://www.ey.com/zh_cn/careers", "专业服务", "B", 80),
    # C: monitored official entries only
    _source(
        "上海华智公考（公众号招聘线索）",
        "https://www.huazhi.cn/",
        "上海公职招考线索",
        "C",
        72,
        adapter_key="wechat_article_lead",
        source_type="公众号线索源",
    ),
    _source("花旗官方招聘（重点监控）", "https://jobs.citi.com/", "外资金融", "C", 78),
    _source("汇丰中国官方招聘（重点监控）", "https://www.hsbc.com/careers", "外资金融", "C", 80),
    _source("摩根大通中国官方招聘（重点监控）", "https://careers.jpmorgan.com/", "外资金融", "C", 78),
    _source("瑞银中国官方招聘（重点监控）", "https://www.ubs.com/global/en/careers.html", "外资金融", "C", 76),
    _source("摩根士丹利中国官方招聘（重点监控）", "https://www.morganstanley.com/careers", "外资金融", "C", 76),
    _source("高盛中国官方招聘（重点监控）", "https://higher.gs.com/", "外资金融", "C", 75),
    _source("毕马威中国官方招聘（重点监控）", "https://kpmg.com/cn/zh/home/careers.html", "专业服务", "C", 80),
    _source("立信会计师事务所官方招聘（重点监控）", "https://www.bdo.com.cn/", "专业服务", "C", 80),
    _source("致同会计师事务所官方招聘（重点监控）", "https://www.grantthornton.cn/", "专业服务", "C", 75),
    _source("天职国际官方招聘（重点监控）", "https://www.bdo.com.cn/", "专业服务", "C", 72),
    _source("信永中和官方招聘（重点监控）", "https://www.shinewing.com/", "专业服务", "C", 72),
    _source("容诚会计师事务所官方招聘（重点监控）", "https://www.rsm.global/china/", "专业服务", "C", 70),
    _source("中国宝武官方招聘（重点监控）", "https://www.baowugroup.com/", "上海央企", "C", 76),
    _source("中国远洋海运官方招聘（重点监控）", "https://www.coscoshipping.com/", "上海央企", "C", 76),
    _source("中国东方航空官方招聘（重点监控）", "https://job.ceair.com/", "上海央企", "C", 76),
    _source("中国商飞官方招聘（重点监控）", "https://www.comac.cc/", "上海央企", "C", 75),
    _source("中国太平保险官方招聘（重点监控）", "https://www.cntaiping.com/", "金融保险", "C", 72),
    _source("国家电网上海招聘入口（重点监控）", "https://zhaopin.sgcc.com.cn/", "上海央企", "C", 72),
    _source("中国中化官方招聘（重点监控）", "https://www.sinochem.com/", "央企全国", "C", 70),
    _source("招商局集团官方招聘（重点监控）", "https://www.cmhk.com/", "央企全国", "C", 70),
    # D: observe in hiring seasons, no job fetches
    _source("中国平安官方招聘（观察库）", "https://job.pingan.com/", "金融保险", "D", 74),
    _source("友邦保险中国官方招聘（观察库）", "https://www.aia.com.cn/zh/careers.html", "金融保险", "D", 70),
    _source("安联中国官方招聘（观察库）", "https://www.allianz.com/en/careers.html", "金融保险", "D", 65),
    _source("西门子中国官方招聘（观察库）", "https://www.siemens.com/cn/zh/company/jobs.html", "外资制造", "D", 72),
    _source("博世中国官方招聘（观察库）", "https://www.bosch.com.cn/careers/", "外资制造", "D", 70),
    _source("施耐德电气中国官方招聘（观察库）", "https://www.se.com/cn/zh/about-us/careers/", "外资制造", "D", 70),
    _source("IBM中国官方招聘（观察库）", "https://www.ibm.com/careers", "外资科技", "D", 68),
    _source("SAP中国官方招聘（观察库）", "https://www.sap.com/china/about/careers.html", "外资科技", "D", 68),
    _source("埃森哲中国官方招聘（观察库）", "https://www.accenture.com/cn-zh/careers", "咨询科技", "D", 72),
    _source("DHL中国官方招聘（观察库）", "https://careers.dhl.com/", "物流供应链", "D", 65),
    _source("马士基中国官方招聘（观察库）", "https://www.maersk.com/careers", "物流供应链", "D", 65),
    _source("罗氏中国官方招聘（观察库）", "https://careers.roche.com/", "医药健康", "D", 70),
    _source("辉瑞中国官方招聘（观察库）", "https://www.pfizer.com/about/careers", "医药健康", "D", 68),
    _source("阿斯利康中国官方招聘（观察库）", "https://careers.astrazeneca.com/", "医药健康", "D", 68),
    _source("拜耳中国官方招聘（观察库）", "https://www.bayer.com/en/careers", "医药健康", "D", 65),
    _source("宝洁中国官方招聘（观察库）", "https://www.pgcareers.com/", "快消", "D", 70),
    _source("联合利华中国官方招聘（观察库）", "https://careers.unilever.com/", "快消", "D", 70),
    _source("欧莱雅中国官方招聘（观察库）", "https://careers.loreal.com/", "快消", "D", 70),
    _source("雀巢中国官方招聘（观察库）", "https://www.nestle.com/jobs", "快消", "D", 65),
    _source("达能中国官方招聘（观察库）", "https://careers.danone.com/", "快消", "D", 65),
)


_LEGACY_SOURCE_RENAMES = {
    "花旗官方招聘（待专用适配）": "花旗官方招聘（重点监控）",
    "上海浦东发展银行官方招聘（待专用适配）": "上海浦东发展银行官方招聘",
}


def ensure_official_source_catalog(session: Session) -> None:
    """Register the curated catalog without changing an operator's health decisions."""
    for legacy_name, catalog_name in _LEGACY_SOURCE_RENAMES.items():
        legacy_source = session.scalar(select(Source).where(Source.name == legacy_name))
        catalog_source = session.scalar(select(Source).where(Source.name == catalog_name))
        if legacy_source is None:
            continue
        if catalog_source is None:
            legacy_source.name = catalog_name
        else:
            session.delete(legacy_source)
        session.flush()
    for definition in OFFICIAL_SOURCE_CATALOG:
        source = session.scalar(select(Source).where(Source.name == definition.name))
        values = definition.__dict__ | {"official_career_url": definition.url}
        if source is None:
            session.add(Source(**values, status="正常", check_frequency_hours=4))
            continue
        for field, value in values.items():
            setattr(source, field, value)
        if definition.library_tier != "A":
            source.is_enabled = False
    session.commit()
