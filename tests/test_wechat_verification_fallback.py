import pytest
from app.services.wechat_leads import _extract_article


def test_verification_html_is_distinct_from_invalid_article():
    with pytest.raises(ValueError, match="微信要求完成验证"):
        _extract_article('<html><body>当前环境异常，完成验证后即可继续访问。去验证</body></html>')
