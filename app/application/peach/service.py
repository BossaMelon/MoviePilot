"""
通过注入端口编排 Peach 任务，冻结提交意图并以相同标识重试入库。
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol
from uuid import uuid4

from app.schemas.context import TorrentInfo
from app.schemas.peach import BridgeRecord, Completion, DownloadReceipt, PeachCandidate, PeachLibraryData, PeachTask


class UnsupportedDownloaderError(ValueError):
    """
    下载器缺少可信完成能力，在外部下载副作用前明确拒绝。
    """


class PeachClientPort(Protocol):
    """
    Peach 入库与馆藏外部接口。
    """

    def normalize_code(self, code: str) -> str:
        """
        使用 Peach 的既有馆藏查询校验并规范化番号。
        """
        ...

    def connected(self) -> bool:
        """
        检查 Peach 专用接口及导入根目录是否就绪。
        """
        ...

    def import_item(self, record: BridgeRecord) -> str:
        """
        以原任务标识提交幂等入库。
        """
        ...

    def library(self, code: Optional[str]) -> PeachLibraryData:
        """
        查询指定番号或全部馆藏。
        """
        ...


class BridgePort(Protocol):
    """
    注入搜索、下载器和 Peach 外部能力；实现不得泄露凭据。
    """

    def normalize_code(self, code: str) -> str:
        """
        使用 Peach 的既有馆藏查询校验并规范化番号。
        """
        ...

    def connected(self) -> bool:
        """
        检查 Peach 专用接口及导入根目录是否就绪。
        """
        ...

    def search(self, code: str, sites: Optional[list[int]]) -> list[TorrentInfo]:
        """
        查询站点原始资源，不识别 TMDB 媒体。
        """
        ...

    def submit(self, torrent: TorrentInfo, directory: Path, downloader: Optional[str]) -> DownloadReceipt:
        """
        提交到独占目录，返回可信下载器与哈希；异常表示结果可能不明确。
        """
        ...

    def completion(self, record: BridgeRecord) -> Completion:
        """
        精确查询实际下载器中的任务完成事实。
        """
        ...

    def import_item(self, record: BridgeRecord) -> str:
        """
        以任务标识幂等入库，返回 imported、pending 或 failed。
        """
        ...

    def library(self, code: Optional[str]) -> PeachLibraryData:
        """
        查询 Peach 馆藏摘要。
        """
        ...


class BridgeStore(Protocol):
    """
    持久任务及带有效期的内部候选；事务锁由实现负责。
    """

    def records(self) -> list[BridgeRecord]:
        """
        返回全部持久任务快照。
        """
        ...

    def reserve(self, candidate_id: str, record: BridgeRecord) -> BridgeRecord:
        """
        原子占有候选；已占有时返回原任务，禁止重复下载。
        """
        ...

    def save(self, record: BridgeRecord) -> None:
        """
        原子保存单任务状态。
        """
        ...

    def candidate(self, candidate_id: str) -> tuple[str, TorrentInfo]:
        """
        读取未过期候选，失效时抛出 ValueError。
        """
        ...

    def add_candidate(self, code: str, torrent: TorrentInfo) -> str:
        """
        保存内部候选并返回不可预测标识。
        """
        ...

    def verified_relative_path(self, root: Path, record: BridgeRecord, path: str) -> str:
        """
        核实真实文件位于根目录和该任务独占目录，无符号链接。
        """
        ...


def normalize_code(code: str) -> str:
    """
    接受常见字母数字番号，拒绝路径、URL 和任意搜索表达式。
    """
    normalized = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,20}(?:[-_][A-Z0-9]{1,20}){0,3}", normalized):
        raise ValueError("番号格式无效")
    normalized = re.sub(r"^([A-Z]{2,20})-?(\d{2,20})$", r"\1-\2", normalized)
    return normalized


class PeachBridgeService:
    """
    组织候选、持久下载意图及下载完成后的 Peach 入库。
    """

    def __init__(self, port: BridgePort, store: BridgeStore, root: Path):
        """
        注入外部能力、持久状态和预先配置的专用下载根目录。
        """
        self.port = port
        self.store = store
        self.root = root

    def search(self, code: str, sites: Optional[list[int]]) -> tuple[str, list[PeachCandidate]]:
        """
        缓存站点原始候选，只公开安全摘要。
        """
        code = self.port.normalize_code(normalize_code(code))
        results = []
        compact = code.replace("-", "").replace("_", "")
        for torrent in self.port.search(code, sites):
            title = (torrent.title or "").upper().replace("_", "-")
            pattern = r"(?<![A-Z0-9])" + r"[- ]?".join(re.escape(part) for part in re.split("[-_]", code)) + r"(?![A-Z0-9])"
            if not re.search(pattern, title) and not re.search(r"(?<![A-Z0-9])" + re.escape(compact) + r"(?![A-Z0-9])", title):
                continue
            candidate_id = self.store.add_candidate(code, torrent)
            results.append(PeachCandidate(
                id=candidate_id, title=torrent.title or code,
                site_name=torrent.site_name or "", size=float(torrent.size or 0),
                seeders=int(torrent.seeders or 0),
            ))
        return code, results

    def tasks(self) -> list[PeachTask]:
        """
        只读取任务，不隐式触发网络或下载器副作用。
        """
        return [record.task for record in self.store.records()]

    def download(self, candidate_id: str, downloader: Optional[str]) -> PeachTask:
        """
        先持久化提交意图再调用下载器，提交不明确时禁止自动重投。
        """
        for previous in self.store.records():
            if previous.candidate_id == candidate_id and not previous.retry_safe:
                return previous.task
        code, torrent = self.store.candidate(candidate_id)
        task_id = uuid4().hex
        record = BridgeRecord(
            task=PeachTask(id=task_id, code=code, title=torrent.title or code,
                           status="needs_attention", message="下载提交意图已保存，若进程中断请在下载器核实，禁止自动重投", created_at=datetime.now(timezone.utc).isoformat()),
            candidate_id=candidate_id,
            resource_key=hashlib.sha256(f"{code}:{torrent.site}:{torrent.enclosure}".encode()).hexdigest(),
            directory=str(self.root / task_id),
        )
        reserved = self.store.reserve(candidate_id, record)
        if reserved.task.id != task_id:
            return reserved.task
        try:
            receipt = self.port.submit(torrent, Path(record.directory), downloader)
        except UnsupportedDownloaderError:
            record.retry_safe = True
            record.task.status = "failed"
            record.task.message = "Peach 闭环仅支持已启用的 qBittorrent 或 Transmission，请配置默认或指定下载器"
            self.store.save(record)
            return record.task
        except Exception:
            record.task.status = "needs_attention"
            record.task.message = "下载提交结果不明确，请在下载器核实，系统不会自动重投"
            self.store.save(record)
            return record.task
        record.downloader = receipt.downloader
        record.info_hash = receipt.info_hash
        record.task.status = "downloading"
        self.store.save(record)
        return record.task

    def sync(self) -> list[PeachTask]:
        """
        检查真实完成事实和路径后幂等入库，网络故障保留可重试状态。
        """
        for record in self.store.records():
            if record.task.status not in ("downloading", "pending_import"):
                continue
            try:
                if record.task.status == "downloading":
                    completion = self.port.completion(record)
                    if not completion.complete:
                        continue
                    record.relative_path = self.store.verified_relative_path(
                        self.root, record, completion.content_path,
                    )
                    record.task.status = "pending_import"
                    self.store.save(record)
                status = self.port.import_item(record)
                if status == "imported":
                    record.task.status = "imported"
                    record.task.message = "Peach 已登记馆藏"
                elif status == "failed":
                    record.task.status = "needs_attention"
                    record.task.message = "Peach 拒绝入库，请检查番号及文件"
                else:
                    record.task.message = "等待 Peach 入库完成"
                self.store.save(record)
            except ValueError:
                record.task.status = "needs_attention"
                record.task.message = "下载完成路径或入库响应不符合安全约束，请人工核实"
                self.store.save(record)
            except Exception:
                record.task.message = "连接暂不可用，下次同步会继续核实"
                self.store.save(record)
        return self.tasks()
