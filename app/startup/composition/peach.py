"""
按部署配置组合 Peach 桥接端口和独立状态文件。
"""

from pathlib import Path

from app.adapters.external.peach.client import PeachHttpClient
from app.adapters.external.peach.store import JsonBridgeStore
from app.application.directory import DirectoryHelper
from app.application.peach.service import PeachBridgeService
from app.chain.peach import PeachBridgeChain
from app.runtime.settings import get_runtime_setting


def build_peach_bridge() -> PeachBridgeService:
    """
    显式配置齐全才启用，状态与普通业务数据库隔离。
    """
    peach_url = get_runtime_setting("PEACH_URL")
    peach_token = get_runtime_setting("PEACH_TOKEN")
    download_root = get_runtime_setting("PEACH_DOWNLOAD_ROOT")
    if not peach_url or not peach_token or not download_root:
        raise ValueError("请配置 Peach 服务、专用令牌和下载根目录")
    root = Path(download_root)
    if not root.is_absolute() or root == Path("/") or any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("Peach 下载根目录必须是独立绝对目录且不能含符号链接")
    for directory in DirectoryHelper().get_download_dirs():
        if directory.storage == "local" and directory.download_path:
            ordinary = Path(directory.download_path).resolve()
            if root.resolve().is_relative_to(ordinary) or ordinary.is_relative_to(root.resolve()):
                raise ValueError("Peach 专用下载目录不能与普通媒体下载目录重叠")
    return PeachBridgeService(
        port=PeachBridgeChain(PeachHttpClient(peach_url, peach_token, get_runtime_setting("PEACH_PUBLIC_URL"))),
        store=JsonBridgeStore(get_runtime_setting("CONFIG_PATH") / "peach-bridge" / "state.json"), root=root,
    )


def sync_peach_downloads() -> None:
    """
    由既有调度器托管有限同步，仅处理持久任务，不触发搜索或新下载。
    """
    build_peach_bridge().sync()
