"""
在一次性、无网络的源码模式容器中验收真实 Python 站点目录。

只用测试站点和虚构凭据替代数据库查询，不连接或修改任何真实站点账号。
与 pytest 的空目录占位不同，本脚本必须加载镜像自带的真实源码。
"""
import argparse
import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from unittest.mock import patch

from app.application.site import sites as source_sites
from app.application.site.sites import SitesHelper
from app.db.models.site import Site
from app.db.oper.site import SiteOper
from app.runtime.extensions.plugin.access import PluginAccessPolicy


def block_connection(_socket, _address):
    """
    任何意外联网请求都让验收立即失败。
    """
    raise AssertionError("本地模式验收禁止发起网络连接")


def configured_sites(_self):
    """
    只返回用于检验凭据透传的模拟站点。
    """
    return [Site(id=1, name="本地模式测试", domain="m-team.io",
                 url="https://xp.m-team.io/", is_active=True,
                 cookie="test-cookie", apikey="test-apikey", token="test-token")]


def configured_site(self, _domain):
    """
    用同一模拟站点满足详情查询。
    """
    return configured_sites(self)[0]


async def async_configured_sites(self):
    """
    为真实扩展的异步列表查询提供无数据库替身。
    """
    return configured_sites(self)


async def async_configured_site(self, domain):
    """
    为真实扩展的异步详情查询提供无数据库替身。
    """
    return configured_site(self, domain)


def main():
    """
    对照原版与本地模式的索引、凭据透传和插件授权边界。
    """
    parser = argparse.ArgumentParser(description="验收真实 Python 站点目录")
    parser.add_argument("--local", action="store_true")
    parser.parse_args()
    socket.socket.connect = block_connection
    SiteOper.list_active = configured_sites
    SiteOper.get_by_domain = configured_site
    SiteOper.async_list_active = async_configured_sites
    SiteOper.async_get_by_domain = async_configured_site
    module_path = Path(source_sites.__file__)
    assert module_path.suffix == ".py", "站点目录必须从 Python 源码加载"
    assert not list(module_path.parent.glob("sites*.so*"))
    assert not list(module_path.parent.glob("sites*.pyd"))
    assert not list(module_path.parent.glob("user.sites*.bin"))
    helper = SitesHelper(repository=SiteOper())
    expected_level = 2
    assert helper.auth_level == expected_level
    indexers = helper.get_indexers()
    indexer = helper.get_indexer("m-team.io")
    assert len(indexers) == 1
    assert bool(indexer) is True
    async_indexers = asyncio.run(helper.async_get_indexers())
    async_indexer = asyncio.run(helper.async_get_indexer("m-team.io"))
    assert len(async_indexers) == len(indexers)
    assert bool(async_indexer) == bool(indexer)
    for result in [indexers[0], indexer, async_indexers[0], async_indexer]:
        assert result["cookie"] == "test-cookie"
        assert result["apikey"] == "test-apikey"
        assert result["token"] == "test-token"
    plugin = type("IndependentPlugin", (), {"auth_level": 99})
    verifications = []

    def reject_key_pair(**keys):
        """
        记录独立插件密钥校验，并拒绝验收用的虚构密钥。
        """
        verifications.append(keys)
        return False

    policy = PluginAccessPolicy(auth_level=lambda: helper.auth_level,
                                verify_keys=reject_key_pair,
                                log=logging.getLogger("local-site-check"))
    assert not policy.check(plugin)
    plugin_with_key = type("LocalModeVerify", (), {
        "auth_level": 99, "plugin_public_key": "test-public-key"})
    with patch.dict(os.environ, {"PLUGIN_LOCALMODEVERIFY_PRIVATE_KEY": "test-private-key"}):
        assert not policy.check(plugin_with_key)
    assert verifications == [{"public_key": "test-public-key", "private_key": "test-private-key"}]
    print(json.dumps({"mode": "source",
                      "auth_level": helper.auth_level,
                      "indexers": len(indexers),
                      "async_indexers": len(async_indexers),
                      "network": "blocked",
                      "checks": "passed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
