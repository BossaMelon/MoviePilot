"""
使用带跨进程锁的私有原子 JSON 状态保存桥接任务，不修改业务数据库。
"""

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.context import TorrentInfo
from app.schemas.peach import BridgeRecord


class _Candidate(BaseModel):
    """
    有时效且仅保存在服务端的站点候选。
    """

    code: str = Field(description="作品番号")
    expires: float = Field(description="过期时间戳")
    torrent: TorrentInfo = Field(description="含实际站点信息的内部资源")


class _State(BaseModel):
    """
    桥接状态文件版本一。
    """

    records: dict[str, BridgeRecord] = Field(default_factory=dict, description="任务集合")
    candidates: dict[str, _Candidate] = Field(default_factory=dict, description="有效候选集合")


class JsonBridgeStore:
    """
    每次读写锁住相同文件，通过原子替换避免中途崩溃损坏状态。
    """

    def __init__(self, path: Path):
        """
        绑定状态文件，不在构造阶段创建或修改文件。
        """
        self.path = path

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """
        在私有父目录创建跨进程锁。
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+b") as stream:
            if sys.platform == "win32":
                # Windows 专用标准库在 Linux 检查环境不存在。
                import msvcrt  # pylint: disable=import-error
                stream.seek(0)
                stream.write(b"0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read(self) -> _State:
        """
        解析现有状态，损坏时失败关闭，禁止覆盖恢复点。
        """
        if not self.path.exists():
            return _State()
        return _State.model_validate_json(self.path.read_text())

    def _write(self, state: _State) -> None:
        """
        同目录写入私有临时文件，刷盘后原子替换。
        """
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(state.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def records(self) -> list[BridgeRecord]:
        """
        在锁内读取任务快照。
        """
        with self._lock():
            return list(self._read().records.values())

    def reserve(self, candidate_id: str, record: BridgeRecord) -> BridgeRecord:
        """
        原子占有候选，同候选仅一个任务可提交下载器。
        """
        with self._lock():
            state = self._read()
            for existing in state.records.values():
                if not existing.retry_safe and (existing.candidate_id == candidate_id or existing.resource_key == record.resource_key):
                    return existing
            state.records[record.task.id] = record
            self._write(state)
            return record

    def save(self, record: BridgeRecord) -> None:
        """
        更新已存在的任务；禁止悄悄创建未知任务。
        """
        with self._lock():
            state = self._read()
            if record.task.id not in state.records:
                raise ValueError("桥接任务不存在")
            state.records[record.task.id] = record
            self._write(state)

    def candidate(self, candidate_id: str) -> tuple[str, TorrentInfo]:
        """
        读取有效候选，凭据从不经由请求体提交。
        """
        with self._lock():
            candidate = self._read().candidates.get(candidate_id)
            if candidate is None or candidate.expires < time.time():
                raise ValueError("候选已过期，请重新搜索")
            return candidate.code, candidate.torrent

    def add_candidate(self, code: str, torrent: TorrentInfo) -> str:
        """
        淘汰过期候选并将数量限制在两千个，避免无限增长。
        """
        with self._lock():
            state = self._read()
            state.candidates = {key: item for key, item in state.candidates.items() if item.expires > time.time()}
            while len(state.candidates) >= 2000:
                del state.candidates[next(iter(state.candidates))]
            candidate_id = uuid4().hex
            state.candidates[candidate_id] = _Candidate(code=code, expires=time.time() + 1800, torrent=torrent)
            self._write(state)
            return candidate_id

    def verified_relative_path(self, root: Path, record: BridgeRecord, path: str) -> str:
        """
        要求路径存在、没有符号链接且属于本任务独占目录，禁止相邻目录和根目录本身。
        """
        target = Path(path)
        if not root.is_absolute() or not target.is_absolute() or not target.exists():
            raise ValueError("下载路径不可用")
        if any(part.is_symlink() for part in (target, *target.parents)):
            raise ValueError("下载路径含符号链接")
        resolved = target.resolve(strict=True)
        directory = Path(record.directory).resolve(strict=True)
        relative = resolved.relative_to(root.resolve(strict=True))
        resolved.relative_to(directory)
        if not relative.parts or directory == root.resolve(strict=True):
            raise ValueError("下载路径范围无效")
        return relative.as_posix()
