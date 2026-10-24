"""
随应用源码交付的站点索引资源兼容接口。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.runtime.log import logger

ResourceVersionProvider = Callable[[], tuple[str, str]]


def _unavailable_resource_versions() -> tuple[str, str]:
    """
    站点能力尚未装配时返回可安全比较的空版本。
    """
    return "0", "0"


_resource_version_provider: ResourceVersionProvider = _unavailable_resource_versions


def configure_resource_version_provider(provider: ResourceVersionProvider) -> None:
    """
    由组合根注入已加载的认证与索引版本，保持适配器不依赖应用层。
    """
    global _resource_version_provider
    _resource_version_provider = provider


def reset_resource_version_provider() -> None:
    """
    撤销当前 lifespan 的资源版本来源，恢复安全空版本。
    """
    global _resource_version_provider
    _resource_version_provider = _unavailable_resource_versions


def get_resource_versions() -> tuple[str, str]:
    """
    读取当前进程已加载的认证资源版本和索引资源版本。
    """
    try:
        return _resource_version_provider()
    # 版本读取失败时不能阻断系统状态接口
    except Exception as error:  # noqa: BLE001
        logger.warning(f"读取站点资源版本失败：{error}")
        return _unavailable_resource_versions()


class ResourceHelper:
    """
    保留旧资源接口；站点索引随应用版本交付，不再独立下载可执行资源。
    """

    _resource_target = Path("app/application/site")

    @classmethod
    def _get_needed_files(cls) -> list[str]:
        """
        返回公开索引文件名，不依赖平台或解释器 ABI。
        """
        return ["sites.json"]

    def get_update_info(
        self,
        *,
        auth_version: str | None = None,
        indexer_version: str | None = None,
    ) -> dict[str, Any] | None:
        """
        索引与应用一起发布，独立资源更新始终无可用更新。
        """
        del auth_version, indexer_version
        return None

    def get_download_files(self, update_info: dict[str, Any]) -> list[dict[str, Any]]:
        """
        拒绝遗留资源下载请求，避免旧清单重新安装站点二进制。
        """
        del update_info
        raise RuntimeError("站点索引随应用源码发布，请更新应用")

    def download_files(
        self,
        files: list[dict[str, Any]],
        destination_dir: Path,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        拒绝独立资源安装，保持源码与索引版本一致。
        """
        del files, destination_dir, on_progress
        raise RuntimeError("站点索引随应用源码发布，请更新应用")

    def check(
        self,
        *,
        auth_version: str | None = None,
        indexer_version: str | None = None,
    ) -> bool:
        """
        保留插件调用接口，不再访问独立二进制仓库。
        """
        return self.get_update_info(
            auth_version=auth_version, indexer_version=indexer_version
        ) is not None
