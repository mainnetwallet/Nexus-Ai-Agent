import pytest

from backend.mcp.base import MCPToolError
from backend.mcp.connectors.browser import BrowserMCPConnector


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, url: str = "https://example.com"):
        self.text = text
        self.status_code = status_code
        self.url = url


class FakeHttpxClient:
    def __init__(self, response: FakeResponse):
        self._response = response
        self.requested_urls = []

    async def get(self, url):
        self.requested_urls.append(url)
        return self._response

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_fetch_url_extracts_text_and_strips_tags():
    html = "<html><head><style>body{color:red}</style></head><body><h1>Hello</h1><p>World</p></body></html>"
    conn = BrowserMCPConnector()
    await conn.connect()
    conn._client = FakeHttpxClient(FakeResponse(html))

    result = await conn.call_tool("fetch_url", {"url": "https://example.com"})
    assert result["status_code"] == 200
    assert "Hello" in result["text"]
    assert "World" in result["text"]
    assert "<h1>" not in result["text"]
    assert "color:red" not in result["text"]


@pytest.mark.asyncio
async def test_fetch_url_raw_html_when_extract_text_false():
    html = "<html><body><p>Raw</p></body></html>"
    conn = BrowserMCPConnector()
    await conn.connect()
    conn._client = FakeHttpxClient(FakeResponse(html))

    result = await conn.call_tool("fetch_url", {"url": "https://example.com", "extract_text": False})
    assert result["content"] == html
    assert "<p>Raw</p>" in result["content"]


@pytest.mark.asyncio
async def test_get_page_links_extracts_href_and_text():
    html = '<a href="https://a.com">Link A</a><a href="/b">Link B</a>'
    conn = BrowserMCPConnector()
    await conn.connect()
    conn._client = FakeHttpxClient(FakeResponse(html))

    result = await conn.call_tool("get_page_links", {"url": "https://example.com"})
    hrefs = {link["href"] for link in result["links"]}
    assert hrefs == {"https://a.com", "/b"}
    texts = {link["text"] for link in result["links"]}
    assert texts == {"Link A", "Link B"}


@pytest.mark.asyncio
async def test_fetch_url_requires_url():
    conn = BrowserMCPConnector()
    await conn.connect()
    conn._client = FakeHttpxClient(FakeResponse(""))
    with pytest.raises(MCPToolError, match="url is required"):
        await conn.call_tool("fetch_url", {"url": ""})


@pytest.mark.asyncio
async def test_current_page_snapshot_without_engine_provider_raises():
    conn = BrowserMCPConnector()
    await conn.connect()
    with pytest.raises(MCPToolError, match="no live browser engine is wired"):
        await conn.call_tool("current_page_snapshot", {})


@pytest.mark.asyncio
async def test_current_page_snapshot_with_no_active_engine_raises():
    conn = BrowserMCPConnector()
    conn.set_engine_provider(lambda: None)
    await conn.connect()
    with pytest.raises(MCPToolError, match="no live browser session is currently active"):
        await conn.call_tool("current_page_snapshot", {})


class FakeSnapshot:
    def __init__(self):
        self.url = "https://example.com/page"
        self.title = "Example Page"
        self.visible_text = "Some visible text"
        self.interactive_elements = [{"role": "button", "name": "Submit"}]


class FakeEngine:
    async def snapshot(self, name_hint="mcp_snapshot"):
        return FakeSnapshot()


@pytest.mark.asyncio
async def test_current_page_snapshot_with_fake_engine_returns_snapshot_fields():
    conn = BrowserMCPConnector(engine_provider=lambda: FakeEngine())
    await conn.connect()
    result = await conn.call_tool("current_page_snapshot", {})
    assert result["url"] == "https://example.com/page"
    assert result["title"] == "Example Page"
    assert result["visible_text"] == "Some visible text"
    assert result["interactive_elements"] == [{"role": "button", "name": "Submit"}]
