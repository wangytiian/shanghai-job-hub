"""Conservative text extraction for public announcement pages."""

from bs4 import BeautifulSoup, Tag


CONTENT_SELECTORS = (
    "main",
    "article",
    "#article-content",
    "#content",
    ".article-content",
    ".detail-content",
    ".content-detail",
    ".TRS_Editor",
)


def extract_article_text(soup: BeautifulSoup) -> str:
    """Return visible announcement text without treating page chrome as evidence."""
    root: Tag | BeautifulSoup | None = None
    for selector in CONTENT_SELECTORS:
        root = soup.select_one(selector)
        if root is not None:
            break
    if root is None:
        root = soup.body or soup
    for item in root.select("script, style, nav, header, footer, aside, form, .nav, .navbar, .footer, .breadcrumb"):
        item.decompose()
    lines: list[str] = []
    for item in root.find_all(["h1", "h2", "h3", "p", "li", "td", "th"]):
        text = item.get_text(" ", strip=True)
        if text and text not in lines:
            lines.append(text)
    if not lines:
        text = root.get_text("\n", strip=True)
        return text
    return "\n".join(lines)
