"""
本地站点模式资源补丁的版本保护、原文件保护和目标文件保护。

真实扩展的索引行为由无网络容器验收；本文件不导入或执行二进制。
"""
import hashlib
from pathlib import Path

import pytest

from scripts import patch_local_site_mode as patch


def test_unknown_binary_is_rejected(tmp_path):
    """
    不认识的版本不能生成输出或改变原文件。
    """
    source = tmp_path / "sites.so"
    target = tmp_path / "local.so"
    source.write_bytes(b"unsupported-resource")
    with pytest.raises(ValueError, match="不支持"):
        patch.write_patch(source, target)
    assert source.read_bytes() == b"unsupported-resource"
    assert not target.exists()


def test_in_place_write_is_rejected(tmp_path):
    """
    防止同一路径或路径别名覆盖原始扩展。
    """
    source = tmp_path / "sites.so"
    source.write_bytes(b"original")
    with pytest.raises(ValueError, match="不同的输出路径"):
        patch.write_patch(source, source)
    assert source.read_bytes() == b"original"


def test_existing_destination_is_preserved(tmp_path, monkeypatch):
    """
    写入前的排他打开保护已有文件。
    """
    source = tmp_path / "sites.so"
    target = tmp_path / "local.so"
    source.write_bytes(b"original")
    target.write_bytes(b"keep")
    monkeypatch.setattr(patch, "patch_bytes", lambda data: b"patched")
    with pytest.raises(FileExistsError):
        patch.write_patch(source, target)
    assert target.read_bytes() == b"keep"
    assert source.read_bytes() == b"original"


def test_destination_symlink_is_preserved(tmp_path, monkeypatch):
    """
    已有目标符号链接不能把写入重定向到其他文件。
    """
    source = tmp_path / "sites.so"
    source.write_bytes(b"original")
    target = tmp_path / "local.so"
    target.symlink_to(source)
    monkeypatch.setattr(patch, "patch_bytes", lambda data: b"patched")
    with pytest.raises(ValueError, match="不同的输出路径"):
        patch.write_patch(source, target)
    assert source.read_bytes() == b"original"


def test_reviewed_resource_changes_only_level_entry(tmp_path):
    """
    可选的真实资源样本只允许改动已审查的函数入口，并支持重复生成。
    """
    resource = Path("/local-site-upstream.so")
    if not resource.is_file():
        pytest.skip("真实资源检查在容器验收中执行")
    before = resource.read_bytes()
    after = patch.patch_bytes(before)
    end = patch.LEVEL_ENTRY_OFFSET + len(patch.LOCAL_ENTRY)
    assert hashlib.sha256(before).hexdigest() == patch.UPSTREAM_SHA256
    assert hashlib.sha256(after).hexdigest() == patch.LOCAL_SHA256
    assert len(before) == len(after)
    assert before[:patch.LEVEL_ENTRY_OFFSET] == after[:patch.LEVEL_ENTRY_OFFSET]
    assert before[end:] == after[end:]
    assert after[patch.LEVEL_ENTRY_OFFSET:end] == patch.LOCAL_ENTRY
    assert patch.patch_bytes(after) == after
    source = tmp_path / "sites.so"
    target = tmp_path / "local.so"
    source.write_bytes(before)
    patch.write_patch(source, target)
    assert source.read_bytes() == before
    assert target.read_bytes() == after
