"""
验证站点公开索引跨平台安装及事务式替换。
"""

import importlib.util
import uuid
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    """
    以隔离模块名加载本地安装脚本，避免测试间共享模块状态。
    """
    module_name = f"moviepilot_local_setup_resources_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("platform_tag", "machine", "python_version"),
    [("linux", "x86_64", "cp314"), ("linux", "aarch64", "cp314t"),
     ("darwin", "arm64", "cp314t"), ("windows", "amd64", "cp313")],
)
def test_filter_resources_files_is_portable(tmp_path, platform_tag, machine, python_version):
    """
    所有平台只选择公开索引，不选择可执行扩展。
    """
    module = load_local_setup_module()
    for filename in ("sites.json", "sites.py", "sites.pyi", "user.sites.v3.bin", "sites.cpython-test.so"):
        (tmp_path / filename).write_text("{}")
    assert [path.name for path in module._filter_resources_files(
        tmp_path, platform_tag, machine, python_version
    )] == ["sites.json"]


def test_copy_resource_files_removes_legacy_binary_only(monkeypatch, tmp_path):
    """
    替换 JSON 并移除遗留二进制，保留源码与其他文件。
    """
    module = load_local_setup_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    data = '{"version":"1","sites":{}}'
    (source / "sites.json").write_text(data)
    for name in ("sites.py", "unrelated.txt", "sites.cpython-test.so", "sites.cp314-win_amd64.pyd", "sites.legacy.dylib", "sites.cpython-test.so.upstream", "user.sites.v3.bin"):
        (target / name).write_text("old")
    monkeypatch.setattr(module, "SITE_RESOURCE_DIR", target)
    assert module.copy_resource_files(source) == ["sites.json"]
    assert sorted(path.name for path in target.iterdir()) == ["sites.json", "sites.py", "unrelated.txt"]
    assert (target / "sites.json").read_text() == data


def test_copy_resource_files_rejects_partial_source(monkeypatch, tmp_path):
    """
    只有旧二进制的来源不能覆盖已有索引。
    """
    module = load_local_setup_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "user.sites.v3.bin").write_bytes(b"binary")
    (target / "sites.json").write_text("old")
    monkeypatch.setattr(module, "SITE_RESOURCE_DIR", target)
    with pytest.raises(RuntimeError, match="sites.json"):
        module.copy_resource_files(source)
    assert (target / "sites.json").read_text() == "old"


@pytest.mark.parametrize("phase", ["staging", "commit"])
def test_copy_resource_files_rolls_back_failed_transaction(monkeypatch, tmp_path, phase):
    """
    暂存或提交失败时保留旧索引、源码和二进制恢复点。
    """
    module = load_local_setup_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "sites.json").write_text('{"version":"1","sites":{}}')
    for name in ("sites.json", "sites.py", "user.sites.v3.bin"):
        (target / name).write_text("old")
    monkeypatch.setattr(module, "SITE_RESOURCE_DIR", target)
    real_copy = module.shutil.copy2
    real_replace = module.os.replace

    def fail_copy(source_file, target_file):
        """
        在资源暂存阶段注入文件系统错误。
        """
        if phase == "staging":
            raise OSError("injected staging failure")
        return real_copy(source_file, target_file)

    def fail_replace(source_file, target_file):
        """
        在索引提交阶段注入文件系统错误。
        """
        if Path(source_file).parent.name == "staging":
            raise OSError("injected commit failure")
        return real_replace(source_file, target_file)

    monkeypatch.setattr(module.shutil, "copy2", fail_copy)
    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        module.copy_resource_files(source)
    assert all((target / name).read_text() == "old" for name in ("sites.json", "sites.py", "user.sites.v3.bin"))
    assert not list(target.glob(".sites-sync-*"))


def test_local_resource_status_requires_source_and_valid_json(monkeypatch, tmp_path):
    """
    就绪状态要求 Python 源码和完整 JSON，不依赖本机 ABI。
    """
    module = load_local_setup_module()
    monkeypatch.setattr(module, "SITE_RESOURCE_DIR", tmp_path)
    (tmp_path / "sites.json").write_text('{"version":"1","sites":{}}')
    assert module.local_resource_status() is False
    (tmp_path / "sites.py").write_text("# source")
    assert module.local_resource_status() is True
    (tmp_path / "sites.json").write_text("{}")
    assert module.local_resource_status() is False


def test_install_resources_defaults_to_bundled_source(monkeypatch, tmp_path):
    """
    默认安装只校验源码目录，不下载资源也不删除应用源码。
    """
    module = load_local_setup_module()
    monkeypatch.setattr(module, "SITE_RESOURCE_DIR", tmp_path)
    monkeypatch.setattr(module, "ensure_local_dirs", lambda: None)
    monkeypatch.setattr(module, "download_file", lambda *_args: pytest.fail("unexpected download"))
    (tmp_path / "sites.py").write_text("# source")
    (tmp_path / "sites.json").write_text('{"version":"1","sites":{}}')
    assert module.install_resources(None, None) == ["sites.json"]
    assert (tmp_path / "sites.py").is_file()
