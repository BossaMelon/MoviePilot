"""验证站点索引随源码离线交付。"""
import importlib.util
from pathlib import Path

import pytest


def load_setup():
    """加载独立安装入口。"""
    spec = importlib.util.spec_from_file_location("source_resource_setup", Path(__file__).resolve().parents[1] / "scripts/local_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_resources_need_no_platform_abi():
    """任意解释器与平台均使用公开 JSON 索引。"""
    module = load_setup()
    assert module._get_runtime_resource_filenames("linux", "x86_64", "cp314t") == ("sites.json",)


def test_source_resource_rejects_invalid_json_before_replacement(tmp_path, monkeypatch):
    """拒绝损坏索引且保留既有文件。"""
    module = load_setup()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "sites.json").write_text("{}")
    (target / "sites.json").write_text("old")
    monkeypatch.setattr(module, "SITE_RESOURCE_DIR", target)
    with pytest.raises(RuntimeError, match="索引"):
        module.copy_resource_files(source)
    assert (target / "sites.json").read_text() == "old"
