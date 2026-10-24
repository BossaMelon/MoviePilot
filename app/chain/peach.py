"""
Peach 桥接使用原始标题搜索和下载器门面，避免进入影视识别和整理历史。
"""

from pathlib import Path
from typing import Optional

from app.application.configuration import get_configured_system_config
from app.application.peach.service import PeachClientPort, UnsupportedDownloaderError
from app.chain.download import DownloadChain
from app.chain.search.facade import SearchChain
from app.domain import torrent as torrent_rules
from app.domain.context import TorrentInfo as DomainTorrentInfo
from app.schemas.context import TorrentInfo
from app.schemas.peach import BridgeRecord, Completion, DownloadReceipt, PeachLibraryData
from app.schemas.system import DownloaderConf
from app.schemas.types import SystemConfigKey


class PeachBridgeChain:
    """
    适配现有搜索下载门面与注入的 Peach 客户端。
    """

    def __init__(self, client: PeachClientPort):
        """
        保存外部客户端接口，不导入具体适配器。
        """
        self.client = client

    def normalize_code(self, code: str) -> str:
        """
        复用 Peach 的权威番号规则，避免本地维护第二套身份解析。
        """
        return self.client.normalize_code(code)

    def connected(self) -> bool:
        """
        检查 Peach 认证和根目录配置是否就绪。
        """
        return self.client.connected()

    def search(self, code: str, sites: Optional[list[int]]) -> list[TorrentInfo]:
        """
        直接按番号搜索，不请求影视身份识别。
        """
        return [TorrentInfo.model_validate(context.torrent_info.to_dict())
                for context in SearchChain().search_by_title(code, sites=sites)
                if context.torrent_info]

    def submit(self, torrent: TorrentInfo, directory: Path, downloader: Optional[str]) -> DownloadReceipt:
        """
        从服务端资源恢复实际凭据，向专用路径下载；不产生影视整理历史。
        """
        configs = [DownloaderConf.model_validate(item) for item in
                   get_configured_system_config().get(SystemConfigKey.Downloaders) or []]
        selected = next((item for item in configs if item.enabled and
                         (item.name == downloader if downloader else item.default)), None)
        if selected is None or selected.type not in {"qbittorrent", "transmission"} or not selected.name:
            raise UnsupportedDownloaderError()
        downloader = selected.name
        resource = DomainTorrentInfo()
        resource.from_dict(torrent.model_dump())
        chain = DownloadChain()
        content, _, _ = chain.download_torrent(resource, source="Peach")
        if not content:
            raise ValueError("站点资源不可下载")
        hashes = torrent_rules.download_hashes(content)
        if hashes and chain.list_torrents(hashs=list(hashes), downloader=downloader, include_all_tags=True):
            raise ValueError("下载器已存在同资源，请人工核实归属")
        directory.mkdir(parents=True, exist_ok=False)
        result = chain.download(content=content, download_dir=directory, cookie=resource.site_cookie,
                                label="peach-bridge", category="peach-bridge", downloader=downloader)
        if not result or not result[0] or not result[1]:
            raise ValueError("下载器未返回可靠任务标识")
        return DownloadReceipt(downloader=result[0], info_hash=result[1])

    def completion(self, record: BridgeRecord) -> Completion:
        """
        仅查询指定下载器与哈希，要求百分百完成并具有可靠内容路径。
        """
        torrents = DownloadChain().list_torrents(hashs=[record.info_hash], downloader=record.downloader,
                                                include_all_tags=True) or []
        for torrent in torrents:
            if torrent.hash != record.info_hash or torrent.downloader != record.downloader:
                continue
            complete = bool(torrent.completion_confirmed and torrent.progress is not None and torrent.progress >= 100
                            and torrent.state in ("completed", "paused"))
            return Completion(complete=complete, content_path=torrent.content_path or str(torrent.path or ""))
        return Completion(complete=False)

    def import_item(self, record: BridgeRecord) -> str:
        """
        将已经验证的任务交给 Peach 幂等入库。
        """
        return self.client.import_item(record)

    def library(self, code: Optional[str]) -> PeachLibraryData:
        """
        返回 Peach 馆藏摘要。
        """
        return self.client.library(code)
