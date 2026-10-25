"""
通过受管 HTTP 访问 Peach 专用集成接口，禁用重定向以防令牌泄露。
"""

from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.adapters.network.http import RequestUtils
from app.schemas.peach import BridgeRecord, PeachLibraryData


class PeachHttpClient:
    """
    使用专用 Bearer 令牌连接管理员配置的 Peach 服务。
    """

    def __init__(self, url: str, token: str, public_url: Optional[str] = None):
        """
        分别验证内部请求地址与浏览器地址，令牌只用于内部接口。
        """
        self.origin = self._validate_base(url)
        self.public_origin = self._validate_base(public_url) if public_url else self.origin
        self.url = self.origin.rstrip("/") + "/api/integrations/moviepilot"
        self.transport = RequestUtils(headers={"Authorization": f"Bearer {token}"}, timeout=20)

    @staticmethod
    def _validate_base(url: str) -> str:
        """
        限制地址为无凭据、查询或片段的有效 HTTP(S) 基址。
        """
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or "@" in parsed.netloc or parsed.query or parsed.fragment
                or "?" in url or "#" in url or "\\" in url
                or any(char.isspace() or ord(char) < 32 for char in url)):
            raise ValueError("Peach 地址格式无效")
        # 同时触发非法端口校验，避免请求阶段才暴露配置错误。
        _ = parsed.port
        return url.rstrip("/") + "/"

    def normalize_code(self, code: str) -> str:
        """
        借助现有馆藏查询取得权威规范番号，作品尚未入库也可返回身份。
        """
        data = self.library(code)
        if not data.code:
            raise ValueError("Peach 未确认番号身份")
        return data.code

    def connected(self) -> bool:
        """
        验证专用接口可达且已通过认证。
        """
        response = self.transport.get_res(self.url + "/status", allow_redirects=False)
        if response is None or response.status_code != 200:
            return False
        payload = response.json()
        return isinstance(payload, dict) and payload.get("configured") is True

    def import_item(self, record: BridgeRecord) -> str:
        """
        使用固定任务标识和安全相对路径重试入库，不传递本机绝对路径。
        """
        response = self.transport.post_res(self.url + "/import", json={
            "request_id": record.task.id, "code": record.task.code,
            "relative_path": record.relative_path, "info_hash": record.info_hash,
        }, allow_redirects=False)
        if response is None or response.status_code >= 500:
            raise ConnectionError("Peach 暂不可用")
        if response.status_code != 200:
            return "failed"
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("request_id") != record.task.id or payload.get("status") not in ("pending", "imported", "failed"):
            raise ValueError("Peach 入库回执不匹配")
        if payload["status"] == "failed":
            raise ConnectionError("Peach 入库执行失败，保留相同任务标识供重试")
        if payload["status"] == "imported":
            items = payload.get("items") or ([payload["item"]] if payload.get("item") else [])
            if not items or not isinstance(items, list) or any(
                    not isinstance(item, dict) or not item.get("id") or item.get("code") != record.task.code
                    for item in items):
                raise ValueError("Peach 入库条目身份不匹配")
        return str(payload["status"])

    def library(self, code: Optional[str]) -> PeachLibraryData:
        """
        验证馆藏外部响应后只返回已声明的摘要字段。
        """
        response = self.transport.get_res(self.url + "/library", params={"code": code} if code else None,
                                          allow_redirects=False)
        if response is not None and response.status_code == 400:
            raise ValueError("番号格式无效")
        if response is None or response.status_code != 200:
            raise ConnectionError("Peach 馆藏暂不可用")
        data = PeachLibraryData.model_validate(response.json())
        for item in data.items:
            if item.peach_url:
                try:
                    if any(char in item.peach_url for char in ("?", "#", "\\")) or any(
                            char.isspace() or ord(char) < 32 for char in item.peach_url):
                        raise ValueError("Peach 馆藏链接格式无效")
                    link = urljoin(self.origin, item.peach_url)
                    self._validate_base(link)
                    parsed = urlsplit(link)
                    internal = urlsplit(self.origin)
                    if (parsed.scheme, parsed.hostname, parsed.port) != (internal.scheme, internal.hostname, internal.port):
                        raise ValueError("Peach 馆藏链接来源无效")
                    public = urlsplit(self.public_origin)
                    item.peach_url = urlunsplit((public.scheme, public.netloc, parsed.path, "", ""))
                except ValueError:
                    item.peach_url = None
        return data
