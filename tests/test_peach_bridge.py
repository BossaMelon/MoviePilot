"""
Peach 闭环、幂等提交、路径边界和 HTTP 权限的隔离回归。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.adapters.external.peach.client import PeachHttpClient
from app.adapters.external.peach.store import JsonBridgeStore
from app.api.dependencies.auth import get_current_active_manage_user, get_current_active_user
from app.api.endpoints import peach
from app.application.peach.service import PeachBridgeService, normalize_code
from app.schemas.context import TorrentInfo
from app.schemas.peach import Completion, DownloadReceipt, PeachLibraryData, PeachLibraryItem


class _Port:
    """
    不访问站点、下载器或网络的可观测外部端口。
    """

    def __init__(self):
        """
        初始化可观察的调用记录。
        """
        self.submissions = []
        self.imports = []
        self.completed = False
        self.fail_submit = False
        self.fail_import = False
        self.path = ""

    def normalize_code(self, code):
        """
        模拟 Peach 规范化已通过语法校验的号码。
        """
        return normalize_code(code)

    def search(self, code, sites):
        """
        返回一个准确资源和两个相似但不相同的番号。
        """
        return [TorrentInfo(title=title, site=1, site_name="fixture", enclosure="https://site.test/download/1", site_cookie="secret")
                for title in [f"{code} 1080p", f"X{code}", f"{code}4"]]

    def submit(self, torrent, directory, downloader):
        """
        记录意图，模拟明确成功或提交中断。
        """
        self.submissions.append((torrent, directory, downloader))
        if self.fail_submit:
            raise ConnectionError("secret internal error")
        directory.mkdir(parents=True)
        content = directory / "ABC-123.mp4"
        content.write_bytes(b"fixture")
        self.path = str(content)
        return DownloadReceipt(downloader="fixture", info_hash="a" * 40)

    def completion(self, record):
        """
        从测试控制的事实返回完成状态。
        """
        return Completion(complete=self.completed, content_path=self.path)

    def import_item(self, record):
        """
        记录相同 request_id 重试，不移动文件。
        """
        self.imports.append(record.task.id)
        if self.fail_import:
            raise ConnectionError("secret")
        return "imported"

    def library(self, code):
        """
        返回已入库作品摘要。
        """
        return PeachLibraryData(items=[PeachLibraryItem(id="1", code=code or "ABC-123", title="fixture")])


@pytest.fixture
def bridge(tmp_path):
    """
    为每个测试隔离任务文件和下载根目录。
    """
    return PeachBridgeService(_Port(), JsonBridgeStore(tmp_path / "state" / "tasks.json"), tmp_path / "downloads")


def _start(bridge):
    """
    搜索并手动选择唯一准确资源。
    """
    _, results = bridge.search("abc123", None)
    assert len(results) == 1
    return bridge.download(results[0].id, None)


def test_closed_loop_only_imports_after_actual_completion(bridge):
    """
    下载未完成不入库，完成后馆藏与任务均可查询。
    """
    task = _start(bridge)
    assert task.status == "downloading"
    assert bridge.sync()[0].status == "downloading"
    assert not bridge.port.imports
    bridge.port.completed = True
    assert bridge.sync()[0].status == "imported"
    assert bridge.port.imports == [task.id]
    assert bridge.port.library(task.code).items[0].code == task.code
    assert Path(bridge.port.path).read_bytes() == b"fixture"
    bridge.sync()
    assert bridge.port.imports == [task.id]


def test_same_resource_across_searches_is_not_submitted_twice(bridge):
    """
    不同候选标识的同站资源仍然复用原任务。
    """
    original = _start(bridge)
    _, results = bridge.search("ABC-123", None)
    assert bridge.download(results[0].id, "different").id == original.id
    assert len(bridge.port.submissions) == 1


def test_ambiguous_submission_survives_restart_without_retry(bridge):
    """
    提交中断后重启保留人工核实状态，不自动重投。
    """
    bridge.port.fail_submit = True
    task = _start(bridge)
    assert task.status == "needs_attention"
    restarted = PeachBridgeService(bridge.port, JsonBridgeStore(bridge.store.path), bridge.root)
    assert restarted.sync()[0].status == "needs_attention"
    assert len(bridge.port.submissions) == 1
    assert "secret" not in task.model_dump_json()


def test_import_retry_uses_same_id_and_retains_files(bridge):
    """
    网络故障停留待入库，相同幂等标识可安全重试。
    """
    task = _start(bridge)
    bridge.port.completed = True
    bridge.port.fail_import = True
    assert bridge.sync()[0].status == "pending_import"
    bridge.port.fail_import = False
    assert bridge.sync()[0].status == "imported"
    assert bridge.port.imports == [task.id, task.id]
    assert Path(bridge.port.path).exists()


@pytest.mark.parametrize("kind", ["outside", "sibling", "symlink", "missing"])
def test_completed_path_must_belong_to_own_directory(bridge, tmp_path, kind):
    """
    完成状态不能绕过文件存在、任务归属和符号链接边界。
    """
    _start(bridge)
    bridge.port.completed = True
    original = Path(bridge.port.path)
    if kind == "outside":
        wrong = tmp_path / "outside.mp4"
        wrong.write_bytes(b"x")
    elif kind == "sibling":
        wrong = bridge.root / "other" / "ABC-123.mp4"
        wrong.parent.mkdir()
        wrong.write_bytes(b"x")
    elif kind == "symlink":
        wrong = original.parent / "link.mp4"
        wrong.symlink_to(original)
    else:
        wrong = original.parent / "missing.mp4"
    bridge.port.path = str(wrong)
    assert bridge.sync()[0].status == "needs_attention"
    assert not bridge.port.imports


@pytest.mark.parametrize("code", ["../ABC-123", "https://test/x", "ABC 123", "a", "ABC;123"])
def test_reject_invalid_code(code):
    """
    番号不能注入路径或任意搜索表达式。
    """
    with pytest.raises(ValueError):
        normalize_code(code)


def test_search_response_does_not_leak_credentials(bridge):
    """
    候选只公开标题与站点摘要。
    """
    _, candidates = bridge.search("ABC-123", None)
    serialized = candidates[0].model_dump_json()
    assert "secret" not in serialized
    assert "download/1" not in serialized


def test_unknown_candidate_cannot_supply_url(bridge):
    """
    任意 URL 不能替代服务端候选。
    """
    with pytest.raises(ValueError):
        bridge.download("https://attacker.test", None)
    assert not bridge.port.submissions


def test_state_corruption_is_not_overwritten(bridge):
    """
    损坏状态失败关闭，不覆盖任务恢复点。
    """
    _start(bridge)
    bridge.store.path.write_text("broken")
    with pytest.raises(ValueError):
        bridge.tasks()
    assert bridge.store.path.read_text() == "broken"


def test_api_permissions_and_envelope(bridge):
    """
    未登录和无管理权限调用均被拒绝，管理员可完成明确操作。
    """
    app = FastAPI()
    app.include_router(peach.router, prefix="/api/v1/peach")
    app.dependency_overrides[peach.get_peach_bridge] = lambda: bridge
    def unauthenticated():
        """
        隔离认证持久层，保留拒绝未登录主体的边界合同。
        """
        raise HTTPException(status_code=401)
    app.dependency_overrides[get_current_active_user] = unauthenticated
    client = TestClient(app)
    assert client.get("/api/v1/peach/tasks").status_code == 401
    user = SimpleNamespace(is_active=True, is_superuser=False, permissions={}, name="reader")
    app.dependency_overrides[get_current_active_user] = lambda: user
    assert client.get("/api/v1/peach/tasks").json()["success"]
    assert client.post("/api/v1/peach/search", json={"code": "ABC-123"}).status_code == 403
    app.dependency_overrides[get_current_active_manage_user] = lambda: user
    result = client.post("/api/v1/peach/search", json={"code": "ABC-123"}).json()
    assert result["success"] and result["data"]["code"] == "ABC-123"
    task = client.post("/api/v1/peach/downloads", json={"candidate_id": result["data"]["results"][0]["id"]}).json()
    assert task["data"]["status"] == "downloading"


def test_http_client_rejects_wrong_receipt_and_cross_origin_link(bridge):
    """
    入库回执必须匹配任务与番号，馆藏链接只允许配置的 Peach 来源。
    """
    _start(bridge)
    record = bridge.store.records()[0]
    client = PeachHttpClient("http://peach.test", "private")
    client.transport = SimpleNamespace(
        post_res=lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: {
            "request_id": record.task.id, "status": "imported", "item": {"id": "1", "code": "WRONG-123"}}),
        get_res=lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: {"items": [
            {"id": "1", "code": "ABC-123", "title": "fixture", "peach_url": "/assets/1"},
            {"id": "2", "code": "ABC-123", "title": "fixture", "peach_url": "http://evil.test/a"}]}),
    )
    with pytest.raises(ValueError):
        client.import_item(record)
    items = client.library(None).items
    assert items[0].peach_url == "http://peach.test/assets/1"
    assert items[1].peach_url is None


def test_chain_raw_search_and_submission_do_not_require_media_identity(monkeypatch, tmp_path):
    """
    真实桥接适配器可直接搜索并提交未识别番号，不进入媒体补全。
    """
    from app.chain import peach as chain_module
    monkeypatch.setattr(chain_module, "get_configured_system_config", lambda: SimpleNamespace(
        get=lambda key: [{"name": "fixture", "type": "qbittorrent", "enabled": True, "default": True}]))
    from app.domain.context import TorrentInfo as DomainTorrentInfo

    resource = DomainTorrentInfo()
    resource.from_dict({"title": "ABC-123", "site": 1, "enclosure": "magnet:?xt=urn:btih:" + "a" * 40})
    calls = []
    monkeypatch.setattr(chain_module, "SearchChain", lambda: SimpleNamespace(
        search_by_title=lambda code, sites: [SimpleNamespace(torrent_info=resource)]))

    def dispatch(**kwargs):
        """
        记录实际低层下载参数，返回可靠回执。
        """
        calls.append(kwargs)
        return "fixture", "a" * 40, None, ""

    monkeypatch.setattr(chain_module, "DownloadChain", lambda: SimpleNamespace(
        download_torrent=lambda torrent, source: (torrent.enclosure, "", []),
        list_torrents=lambda **kwargs: [], download=dispatch))
    chain = chain_module.PeachBridgeChain(SimpleNamespace())
    found = chain.search("ABC-123", [1])
    receipt = chain.submit(found[0], tmp_path / "dedicated", "fixture")
    assert receipt.info_hash == "a" * 40
    assert calls[0]["download_dir"] == tmp_path / "dedicated"
    assert calls[0]["label"] == "peach-bridge"
    assert "media" not in calls[0]


def test_chain_existing_hash_does_not_submit(monkeypatch, tmp_path):
    """
    即使候选链接变化，相同真实种子已存在时也不接管或再次提交。
    """
    from app.chain import peach as chain_module
    monkeypatch.setattr(chain_module, "get_configured_system_config", lambda: SimpleNamespace(
        get=lambda key: [{"name": "fixture", "type": "qbittorrent", "enabled": True, "default": True}]))
    calls = []
    monkeypatch.setattr(chain_module, "DownloadChain", lambda: SimpleNamespace(
        download_torrent=lambda *args, **kwargs: ("magnet:?xt=urn:btih:" + "a" * 40, "", []),
        list_torrents=lambda **kwargs: [SimpleNamespace(hash="a" * 40)],
        download=lambda **kwargs: calls.append(kwargs)))
    chain = chain_module.PeachBridgeChain(SimpleNamespace())
    with pytest.raises(ValueError):
        chain.submit(TorrentInfo(title="ABC-123", enclosure="https://site.test/x"), tmp_path / "task", None)
    assert not calls
    assert not (tmp_path / "task").exists()


@pytest.mark.parametrize("progress,state,task_hash,downloader,complete", [
    (100, "completed", "a" * 40, "fixture", True),
    (100, "paused", "a" * 40, "fixture", True),
    (99, "completed", "a" * 40, "fixture", False),
    (100, "downloading", "a" * 40, "fixture", False),
    (100, "completed", "b" * 40, "fixture", False),
    (100, "completed", "a" * 40, "other", False),
])
def test_chain_completion_checks_actual_identity(bridge, monkeypatch, progress, state, task_hash, downloader, complete):
    """
    完成判定依赖下载器实际身份、百分比和统一状态，不只看百分比。
    """
    from app.chain import peach as chain_module
    monkeypatch.setattr(chain_module, "get_configured_system_config", lambda: SimpleNamespace(
        get=lambda key: [{"name": "fixture", "type": "qbittorrent", "enabled": True, "default": True}]))
    from app.schemas.transfer import DownloaderTorrent
    _start(bridge)
    record = bridge.store.records()[0]
    monkeypatch.setattr(chain_module, "DownloadChain", lambda: SimpleNamespace(list_torrents=lambda **kwargs: [
        DownloaderTorrent(hash=task_hash, downloader=downloader, progress=progress,
                          state=state, content_path=bridge.port.path, completion_confirmed=True)]))
    result = chain_module.PeachBridgeChain(SimpleNamespace()).completion(record)
    assert result.complete is complete
    if complete:
        assert result.content_path == bridge.port.path


def test_factory_rejects_overlapping_ordinary_download_root(monkeypatch, tmp_path):
    """
    专用目录不能落入普通媒体下载监控范围。
    """
    from app.startup.composition import peach as composition
    values = {"PEACH_URL": "http://peach.test", "PEACH_TOKEN": "private", "PEACH_DOWNLOAD_ROOT": str(tmp_path / "ordinary" / "peach")}
    monkeypatch.setattr(composition, "get_runtime_setting", values.__getitem__)
    monkeypatch.setattr(composition, "DirectoryHelper", lambda: SimpleNamespace(get_download_dirs=lambda: [
        SimpleNamespace(storage="local", download_path=str(tmp_path / "ordinary"))]))
    with pytest.raises(ValueError, match="重叠"):
        composition.build_peach_bridge()


def test_regular_transfer_rejects_peach_owned_files(monkeypatch, tmp_path):
    """
    普通整理的统一执行入口拒绝 Peach 独占文件，避免双边同时接管。
    """
    from app.chain.transfer import workflow
    from app.schemas.file import FileItem
    root = tmp_path / "peach"
    monkeypatch.setattr(workflow, "get_runtime_setting", lambda key, default=None: str(root))
    result = workflow.TransferWorkflowOwner._run_transfer_workflow(
        SimpleNamespace(), FileItem(storage="local", path=str(root / "task" / "ABC-123.mp4"), type="file"))
    assert result[0] is False
    assert "Peach" in result[1]


def test_scheduler_disabled_and_enabled_catalog():
    """
    未装配时不登记任务，装配后使用既有任务目录而非额外线程。
    """
    from app.scheduler.catalog import _peach_job_specs
    assert _peach_job_specs(SimpleNamespace(peach_sync=None)) == ()
    calls = []
    jobs = _peach_job_specs(SimpleNamespace(peach_sync=lambda: calls.append(True)))
    assert len(jobs) == 1
    assert jobs[0].job_id == "peach_sync"


def test_sync_without_owned_tasks_does_not_search_or_download(bridge):
    """
    空队列同步不会搜索站点、提交下载或访问 Peach 入库。
    """
    assert bridge.sync() == []
    assert bridge.port.submissions == []
    assert bridge.port.imports == []


@pytest.mark.parametrize("raw_state,expected", [
    ("uploading", True), ("pausedUP", True), ("stoppedUP", True),
    ("error", False), ("missingFiles", False), ("checkingUP", False),
    ("checkingResumeData", False), ("metaDL", False), ("moving", False), ("future-state", False),
])
def test_qbittorrent_real_dto_requires_known_completion(monkeypatch, raw_state, expected):
    """
    真实 qBittorrent 投影不把状态兜底 completed 误作完成确认。
    """
    from app.modules.qbittorrent import QbittorrentModule
    module = QbittorrentModule.__new__(QbittorrentModule)
    raw = {"name": "ABC-123", "content_path": "/downloads/ABC-123.mp4", "hash": "a" * 40,
           "total_size": 1024, "completed": 1024, "progress": 1, "amount_left": 0, "state": raw_state}
    server = SimpleNamespace(get_torrents=lambda **kwargs: ([raw], False))
    monkeypatch.setattr(module, "get_instances", lambda: {"fixture": server})
    monkeypatch.setattr(module, "normalize_return_path", lambda path, name: str(path))
    dto = module.list_torrents(hashs=["a" * 40], include_all_tags=True)[0]
    assert dto.completion_confirmed is expected


@pytest.mark.parametrize("raw_state,error,left,expected", [
    ("seeding", 0, 0, True), ("stopped", 0, 0, True), ("seed pending", 0, 0, True),
    ("checking", 0, 0, False), ("check pending", 0, 0, False), ("future-state", 0, 0, False),
    ("seeding", 3, 0, False), ("seeding", 0, 1, False), ("downloading", 0, 0, False),
])
def test_transmission_real_dto_requires_known_completion(monkeypatch, raw_state, error, left, expected):
    """
    真实 Transmission SDK 状态与错误/剩余字节共同决定完成事实。
    """
    from app.modules.transmission import TransmissionModule
    module = TransmissionModule.__new__(TransmissionModule)
    raw = SimpleNamespace(name="ABC-123", hashString="a" * 40, download_dir="/downloads", status=raw_state,
                          total_size=1024, progress=100, left_until_done=left, error=error)
    server = SimpleNamespace(get_torrents=lambda **kwargs: ([raw], False))
    monkeypatch.setattr(module, "get_instances", lambda: {"fixture": server})
    monkeypatch.setattr(module, "normalize_return_path", lambda path, name: str(path))
    dto = module.list_torrents(hashs=["a" * 40], include_all_tags=True)[0]
    assert dto.completion_confirmed is expected


def test_rtorrent_real_dto_does_not_claim_missing_hashchecking_fact(monkeypatch):
    """
    缺少原始检查状态的 rTorrent 即便完成位为一，也保持失败关闭。
    """
    from app.modules.rtorrent import RtorrentModule
    module = RtorrentModule.__new__(RtorrentModule)
    raw = {"name": "ABC-123", "content_path": "/downloads/ABC-123.mp4", "hash": "a" * 40,
           "total_size": 1024, "completed": 1024, "progress": 100, "complete": 1, "state": 1}
    server = SimpleNamespace(get_torrents=lambda **kwargs: ([raw], False))
    monkeypatch.setattr(module, "get_instances", lambda: {"fixture": server})
    monkeypatch.setattr(module, "normalize_return_path", lambda path, name: str(path))
    dto = module.list_torrents(hashs=["a" * 40], include_all_tags=True)[0]
    assert dto.completion_confirmed is False


def test_client_execution_failed_receipt_retries_same_task(bridge):
    """
    Peach 执行失败的 200 回执可重试，真实客户端不将其永久拒绝。
    """
    task = _start(bridge)
    bridge.port.completed = True
    client = PeachHttpClient("http://peach.test", "private")
    calls = []

    def post(*args, **kwargs):
        """
        模拟一次数据库异常回执后恢复，观察幂等键一致。
        """
        calls.append(kwargs["json"])
        status = "failed" if len(calls) == 1 else "imported"
        return SimpleNamespace(status_code=200, json=lambda: {
            "request_id": task.id, "status": status, "item": {"id": "1", "code": task.code}})

    client.transport = SimpleNamespace(post_res=post)
    bridge.port.import_item = client.import_item
    assert bridge.sync()[0].status == "pending_import"
    assert bridge.sync()[0].status == "imported"
    assert [item["request_id"] for item in calls] == [task.id, task.id]
    assert Path(bridge.port.path).exists()


def test_client_permanent_http_rejection_requires_attention(bridge):
    """
    Peach 参数或授权错误不会被当作执行失败无限重试。
    """
    _start(bridge)
    bridge.port.completed = True
    client = PeachHttpClient("http://peach.test", "private")
    calls = []

    def post(*args, **kwargs):
        """
        记录被永久拒绝的提交次数。
        """
        calls.append(kwargs)
        return SimpleNamespace(status_code=400)

    client.transport = SimpleNamespace(post_res=post)
    bridge.port.import_item = client.import_item
    assert bridge.sync()[0].status == "needs_attention"
    bridge.sync()
    assert len(calls) == 1


def test_unsupported_downloader_is_rejected_before_external_effect(bridge, monkeypatch):
    """
    rTorrent 与未知类型在下载种子、提交任务及创建内容目录之前明确拒绝。
    """
    from app.application.peach.service import UnsupportedDownloaderError
    from app.chain import peach as chain_module
    monkeypatch.setattr(chain_module, "get_configured_system_config", lambda: SimpleNamespace(
        get=lambda key: [{"name": "legacy", "type": "rtorrent", "enabled": True, "default": True}]))
    monkeypatch.setattr(chain_module, "DownloadChain", lambda: pytest.fail("不得访问下载器"))
    chain = chain_module.PeachBridgeChain(SimpleNamespace())
    with pytest.raises(UnsupportedDownloaderError):
        chain.submit(TorrentInfo(title="ABC-123"), bridge.root / "unsupported", None)
    assert not (bridge.root / "unsupported").exists()
    bridge.port.submit = chain.submit
    task = _start(bridge)
    assert task.status == "failed"
    assert "qBittorrent" in task.message


def test_supported_reselection_after_known_no_effect_rejection(bridge):
    """
    将不支持下载器改为受支持下载器后可重选同资源，保留失败历史且只实际提交一次。
    """
    from app.application.peach.service import UnsupportedDownloaderError
    original_submit = bridge.port.submit

    def reject(*args, **kwargs):
        """
        模拟默认 rTorrent 在任何外部副作用之前拒绝。
        """
        raise UnsupportedDownloaderError()

    bridge.port.submit = reject
    rejected = _start(bridge)
    assert rejected.status == "failed"
    assert bridge.port.submissions == []
    bridge.port.submit = original_submit
    accepted = _start(bridge)
    assert accepted.id != rejected.id
    assert accepted.status == "downloading"
    assert len(bridge.port.submissions) == 1
    assert len(bridge.tasks()) == 2
    repeated = _start(bridge)
    assert repeated.id == accepted.id
    assert len(bridge.port.submissions) == 1


def test_chain_rejects_legacy_completed_state_without_confirmed_fact(bridge, monkeypatch):
    """
    第三方或旧下载器仅提供 completed 与 100% 不足以触发入库。
    """
    from app.chain import peach as chain_module
    from app.schemas.transfer import DownloaderTorrent
    _start(bridge)
    dto = DownloaderTorrent(hash="a" * 40, downloader="fixture", progress=100,
                            state="completed", content_path=bridge.port.path)
    monkeypatch.setattr(chain_module, "DownloadChain", lambda: SimpleNamespace(list_torrents=lambda **kwargs: [dto]))
    assert not chain_module.PeachBridgeChain(SimpleNamespace()).completion(bridge.store.records()[0]).complete


@pytest.mark.parametrize("link,expected", [
    ("/assets/1", "http://localhost:18900/assets/1"),
    ("http://peach.test/assets/1", "http://localhost:18900/assets/1"),
    ("//evil.test/assets/1", None),
    ("http://user@peach.test/assets/1", None),
    ("http://peach.test/assets/1?secret=1", None),
    ("http://peach.test/assets/1#fragment", None),
    ("http://peach.test/assets/1?", None),
    ("https://peach.test/assets/1", None),
    ("/assets/1#", None),
    ("/assets/1?", None),
    ("http://@peach.test/assets/1", None),
])
def test_http_library_rewrites_valid_internal_links(link, expected):
    """
    浏览器地址只替换已通过内部同源校验的馆藏链接。
    """
    client = PeachHttpClient("http://peach.test", "private", "http://localhost:18900")
    calls = []

    def get_res(url, **kwargs):
        """
        捕获真实请求目标并返回受控馆藏响应。
        """
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"items": [
            {"id": "1", "code": "ABC-123", "title": "fixture", "peach_url": link}]})

    client.transport = SimpleNamespace(get_res=get_res)
    assert client.library(None).items[0].peach_url == expected
    assert calls == [("http://peach.test/api/integrations/moviepilot/library", {
        "params": None, "allow_redirects": False})]


@pytest.mark.parametrize("url", [
    "ftp://public.test", "http://user:secret@public.test", "http://public.test/?x=1",
    "http://public.test/#x", "http://public.test:invalid", "http://public.test/?",
])
def test_http_client_rejects_invalid_public_url(url):
    """
    无效浏览器地址在构造客户端时拒绝，避免产生不安全链接。
    """
    with pytest.raises(ValueError):
        PeachHttpClient("http://peach.test", "private", url)


def test_http_client_token_stays_on_internal_transport(monkeypatch):
    """
    浏览器地址不改变令牌传输目标，留空时保留既有馆藏地址。
    """
    from app.adapters.external.peach import client as client_module
    captured = []
    monkeypatch.setattr(client_module, "RequestUtils", lambda **kwargs: captured.append(kwargs) or SimpleNamespace())
    client = PeachHttpClient("http://peach.test", "private", "http://localhost:18900")
    assert client.url == "http://peach.test/api/integrations/moviepilot"
    assert captured[0]["headers"] == {"Authorization": "Bearer private"}
    assert PeachHttpClient("http://peach.test", "private", "").public_origin == "http://peach.test/"
