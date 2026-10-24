"""
验证 MoviePilot 启动前即可使用的安装向导行为。
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
import types
import uuid
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    """
    加载独立的安装模块，隔离向导测试中的模块状态。
    """
    module_name = f"moviepilot_local_setup_wizard_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_llm_provider_module_loads_when_repository_root_is_not_on_import_path(
    monkeypatch,
):
    """
    独立安装器必须让模型提供方能够导入应用模块。
    """
    module = load_local_setup_module()
    repository_root = str(module.ROOT)
    monkeypatch.setattr(
        module.sys,
        "path",
        [entry for entry in sys.path if entry != repository_root],
    )

    provider_module = module._load_llm_provider_module()

    assert module.sys.path[0] == repository_root
    assert hasattr(provider_module, "LLMProviderManager")
    monkeypatch.delitem(module.sys.modules, provider_module.__name__)


def test_startup_python_resolution_preserves_virtualenv_symlink(
    monkeypatch, tmp_path: Path
):
    """
    自动启动必须调用虚拟环境启动器，不能绕过环境使用基础解释器。
    """
    module = load_local_setup_module()
    venv_dir = tmp_path / "venv"
    python_launcher = module.get_venv_python(venv_dir)
    python_launcher.parent.mkdir(parents=True)
    python_launcher.symlink_to(Path(sys.executable))
    expected_path = python_launcher.absolute()
    monkeypatch.setattr(
        module,
        "_can_run_moviepilot_cli",
        lambda candidate: candidate == expected_path,
    )

    runtime_python = module._resolve_runtime_python_for_startup(
        python_launcher, venv_dir
    )

    assert runtime_python == expected_path
    assert runtime_python.is_symlink()


def test_startup_python_resolution_prefers_project_runtime_launcher(
    monkeypatch, tmp_path: Path
):
    """
    自动启动优先使用项目运行时启动器，避免选择外部解释器。
    """
    module = load_local_setup_module()
    venv_dir = tmp_path / "venv"
    runtime_launcher = module.get_venv_bin_dir(venv_dir) / "moviepilot-python"
    runtime_launcher.parent.mkdir(parents=True)
    runtime_launcher.symlink_to(Path(sys.executable))
    expected_path = runtime_launcher.absolute()
    external_python = tmp_path / "external-python"
    monkeypatch.setattr(
        module,
        "_can_run_moviepilot_cli",
        lambda candidate: candidate == expected_path,
    )

    runtime_python = module._resolve_runtime_python_for_startup(
        external_python, venv_dir
    )

    assert runtime_python == expected_path


def test_directory_config_keeps_download_path_as_downloader_path(monkeypatch):
    """
    向导保留下载器路径，不将其作为宿主路径解析。
    """
    module = load_local_setup_module()
    text_prompts: list[tuple[str, str]] = []
    path_prompts: list[str] = []
    monkeypatch.setattr(module, "print_step", lambda _message: None)

    def prompt_text(label: str, default: str) -> str:
        """
        记录下载器路径提示，并返回容器内的路径。
        """
        text_prompts.append((label, default))
        return "/downloads"

    def prompt_path(label: str, default: Path) -> str:
        """
        提供宿主侧媒体库路径。
        """
        assert isinstance(default, Path)
        path_prompts.append(label)
        return "/video"

    monkeypatch.setattr(module, "_prompt_text", prompt_text)
    monkeypatch.setattr(module, "_prompt_path", prompt_path)
    monkeypatch.setattr(module, "_prompt_choice", lambda *_args, **_kwargs: "copy")

    directory_config = module._collect_directory_config()

    assert directory_config["download_path"] == "/downloads"
    assert directory_config["library_path"] == "/video"
    assert text_prompts[0][0] == "下载器中的下载目录根路径"
    assert path_prompts == ["媒体库目录"]


def test_setup_wizard_finishes_without_platform_qualification(monkeypatch):
    """
    源码安装向导完成管理员和服务配置，不收集资源站点资格凭据。
    """
    module = load_local_setup_module()
    prompts = []
    monkeypatch.setattr(module, "_is_interactive", lambda: True)
    monkeypatch.setattr(module, "print_step", lambda _message: None)
    monkeypatch.setattr(module, "read_env_value", lambda _key: "existing-api-token-1234")
    monkeypatch.setattr(module, "ensure_api_token", lambda **_kwargs: "existing-api-token-1234")

    def accept_prompt(label, **_kwargs):
        """
        记录向导交互，允许继续使用已有 API 令牌。
        """
        prompts.append(label)
        return True

    monkeypatch.setattr(module, "_prompt_yes_no", accept_prompt)
    monkeypatch.setattr(module, "_collect_superuser_config", lambda **_kwargs: {"SUPERUSER": "admin"})
    monkeypatch.setattr(module, "_collect_agent_config", lambda **_kwargs: {})
    for name in ("database", "directory", "downloader", "media_server", "notification", "autostart"):
        monkeypatch.setattr(module, f"_collect_{name}_config", lambda: {})
    result = module.run_setup_wizard(force_token=False)
    assert result["env_settings"]["SUPERUSER"] == "admin"
    assert result["api_token"] == "existing-api-token-1234"
    assert "site_auth" not in result
    assert prompts == ["检测到现有 API_TOKEN，是否继续使用"]


def test_retired_auth_site_cli_is_rejected(monkeypatch):
    """
    安装入口不再提供平台资格认证字段查询子命令。
    """
    module = load_local_setup_module()
    monkeypatch.setattr(module.sys, "argv", ["local_setup.py", "query-auth-sites", "--output-json-file", "/unused"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2


def test_apply_config_does_not_create_downloader_internal_path(monkeypatch):
    """
    应用安装配置时只创建宿主侧媒体目录。
    """
    module = load_local_setup_module()
    created_directories: list[str] = []
    original_import = builtins.__import__

    def fake_path(value: str):
        """
        记录目录创建请求，不修改宿主文件系统。
        """

        def mkdir(*, parents: bool, exist_ok: bool) -> None:
            """
            记录请求创建的目录。
            """
            assert parents and exist_ok
            created_directories.append(value)

        return types.SimpleNamespace(mkdir=mkdir)

    def fail_before_runtime_initialization(name: str, *args, **kwargs):
        """
        路径创建阶段结束后中止，避免进入数据库初始化。
        """
        if name == "app.application.classification.reference":
            raise ModuleNotFoundError("stop after path setup")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(module, "Path", fake_path)
    monkeypatch.setattr(builtins, "__import__", fail_before_runtime_initialization)

    with pytest.raises(RuntimeError, match="尚未安装 MoviePilot 运行依赖"):
        module._apply_local_system_config_inner(
            {
                "directories": [
                    {"download_path": "/downloads", "library_path": "/video"}
                ]
            }
        )

    assert created_directories == ["/video"]
