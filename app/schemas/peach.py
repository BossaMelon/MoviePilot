"""
Peach 桥接的请求、任务与馆藏公开合同。
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class PeachSearchRequest(BaseModel):
    """
    按番号检索实际站点。
    """

    code: str = Field(min_length=1, max_length=64, description="作品番号")
    sites: Optional[list[int]] = Field(default=None, description="限定站点编号")


class PeachDownloadRequest(BaseModel):
    """
    选择服务端缓存候选并提交下载。
    """

    candidate_id: str = Field(description="服务端候选标识")
    downloader: Optional[str] = Field(default=None, description="下载器名称")


class PeachCandidate(BaseModel):
    """
    不携带站点凭据与资源链接的候选摘要。
    """

    id: str = Field(description="候选标识")
    title: str = Field(description="资源标题")
    site_name: str = Field(description="站点名称")
    size: float = Field(default=0, description="资源字节数")
    seeders: int = Field(default=0, description="做种数量")


class PeachSearchData(BaseModel):
    """
    番号搜索结果。
    """

    code: str = Field(description="规范化番号")
    results: list[PeachCandidate] = Field(default_factory=list, description="资源候选")


class PeachTask(BaseModel):
    """
    桥接任务公开状态。
    """

    id: str = Field(description="幂等任务标识")
    code: str = Field(description="规范化番号")
    title: str = Field(description="资源标题")
    status: Literal["submitted", "downloading", "pending_import", "imported", "failed", "needs_attention"] = Field(description="任务状态")
    message: str = Field(default="", description="可公开状态说明")
    created_at: str = Field(description="任务创建时间")


class PeachTasksData(BaseModel):
    """
    桥接任务列表。
    """

    tasks: list[PeachTask] = Field(default_factory=list, description="任务列表")


class PeachLibraryItem(BaseModel):
    """
    Peach 馆藏条目摘要。
    """

    id: str = Field(description="馆藏标识")
    code: str = Field(description="作品番号")
    title: str = Field(description="作品标题")
    peach_url: Optional[str] = Field(default=None, description="Peach 作品页面")


class PeachLibraryData(BaseModel):
    """
    Peach 馆藏查询结果。
    """

    code: Optional[str] = Field(default=None, description="Peach 规范化番号，无筛选时为空")
    items: list[PeachLibraryItem] = Field(default_factory=list, description="馆藏条目")


class PeachStatusData(BaseModel):
    """
    不包含密钥与连接地址的桥接状态。
    """

    configured: bool = Field(description="必要配置是否齐全")
    connected: bool = Field(default=False, description="Peach 接口是否可达")
    message: str = Field(default="", description="连接状态说明")


class BridgeRecord(BaseModel):
    """
    任务的内部持久状态，不向浏览器泄露下载路径和任务哈希。
    """

    task: PeachTask = Field(description="公开任务状态")
    candidate_id: str = Field(description="原候选标识")
    resource_key: str = Field(description="同站资源稳定摘要，用于跨搜索去重")
    downloader: Optional[str] = Field(default=None, description="实际下载器")
    info_hash: str = Field(default="", description="实际下载任务标识")
    directory: str = Field(description="本任务独占下载目录")
    relative_path: str = Field(default="", description="已验证的相对内容路径")
    retry_safe: bool = Field(default=False, description="已证明没有下载副作用的拒绝，可解除资源占有后重选")


class DownloadReceipt(BaseModel):
    """
    下载器提交回执。
    """

    downloader: str = Field(description="实际下载器")
    info_hash: str = Field(description="实际下载任务标识")


class Completion(BaseModel):
    """
    下载器报告的完成事实。
    """

    complete: bool = Field(description="下载是否完整完成")
    content_path: str = Field(default="", description="下载内容绝对路径")
