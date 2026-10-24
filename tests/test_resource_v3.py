"""
验证站点资源版本选择、下载与安装行为。
"""

from pathlib import Path

import pytest

from app.adapters.system.resource import (
    ResourceHelper,
)
from app.startup.composition import resource as resource_composition
from app.startup.initializers import modules as modules_initializer

ROOT_DIR = Path(__file__).resolve().parents[1]


def test_resource_helper_uses_source_index_only():
    """
    独立更新器只认公开 JSON，不访问二进制仓库。
    """
    assert ResourceHelper._get_needed_files() == ["sites.json"]
    assert ResourceHelper._resource_target == Path("app/application/site")
    assert ResourceHelper().get_update_info(auth_version="0", indexer_version="0") is None
    assert ResourceHelper().check() is False


def test_resource_helper_rejects_legacy_download(tmp_path):
    """
    旧插件也不能通过下载兼容入口恢复二进制。
    """
    with pytest.raises(RuntimeError, match="源码"):
        ResourceHelper().get_download_files({"files": [{"name": "sites.bad.so"}]})
    with pytest.raises(RuntimeError, match="源码"):
        ResourceHelper().download_files([], tmp_path)


def test_startup_resource_hook_only_configures_version_provider(monkeypatch):
    """
    启动组合层只注册资源版本读取器，不在进程启动后下载或重启。
    """
    providers = []
    monkeypatch.setattr(
        resource_composition,
        "configure_resource_version_provider",
        lambda provider: providers.append(provider),
    )

    resource_composition.configure_site_resource_versions(lambda: ("1", "2"))

    assert len(providers) == 1
    assert providers[0]() == ("1", "2")


def test_startup_resource_update_hook_is_noop(monkeypatch):
    """
    旧启动入口保留兼容性，但不再触发资源检查、安装或重启。
    """
    modules_initializer.update_resources()


def test_install_and_docker_paths_do_not_reference_v2_resources():
    """
    本地安装和 Docker 资源流程不得包含 V2 资源回退。
    """
    paths = [
        ROOT_DIR / "scripts" / "local_setup.py",
        ROOT_DIR / "docker" / "Dockerfile",
        ROOT_DIR / "docker" / "update.sh",
    ]

    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert "resources.v2" not in content
        assert "user.sites.v2.bin" not in content
        assert "sites.json" in content
        assert "app/application/site" in content


def test_docker_entrypoint_does_not_sync_updater_as_a_special_case():
    """
    容器控制脚本必须由 launcher 统一固化，不能单独替换 updater。
    """
    content = (ROOT_DIR / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    assert "mp_update.sh" not in content
    assert 'source "${MP_CONTROL_DIR:-/usr/local/lib/moviepilot/control}/update.sh"' in content


def test_v3_release_workflows_use_main_wiki_and_isolated_images():
    """
    V3 正式版和 Beta 构建应读取主 Wiki 分支并保持镜像仓库隔离。
    """
    build_workflow = (ROOT_DIR / ".github" / "workflows" / "build-v3.yml").read_text(encoding="utf-8")
    beta_workflow = (ROOT_DIR / ".github" / "workflows" / "beta.yml").read_text(encoding="utf-8")

    assert "name: MoviePilot Builder v3" in build_workflow
    assert "      - v3" in build_workflow
    assert "          repository: jxxghp/MoviePilot-Wiki\n          ref: main" in build_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot\n" in build_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v3" in build_workflow
    assert "ghcr.io/${{ github.repository }}-v3" in build_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v2" not in build_workflow
    assert "python3 scripts/release_notes.py" in build_workflow
    assert '--source "$SOURCE_COMMIT"' in build_workflow

    assert "          repository: jxxghp/MoviePilot-Wiki\n          ref: main" in beta_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v3" in beta_workflow
    assert "ghcr.io/${{ github.repository }}-v3" in beta_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v2" not in beta_workflow


def test_frontend_install_and_update_only_use_fork():
    """
    构建、安装与更新入口只能获取 fork 前端，避免恢复平台认证界面。
    """
    for relative in ("app/adapters/system/update.py", "scripts/local_setup.py", "docker/Dockerfile", "docker/update.sh"):
        source = (ROOT_DIR / relative).read_text(encoding="utf-8")
        assert "BossaMelon/MoviePilot-Frontend" in source
        assert "jxxghp/MoviePilot-Frontend" not in source
    overlay = (ROOT_DIR / "docker/Dockerfile.local-site-mode").read_text(encoding="utf-8")
    assert "COPY frontend-dist/ /public/" in overlay
