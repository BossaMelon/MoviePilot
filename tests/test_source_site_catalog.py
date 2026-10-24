"""
源码站点目录、配置合并、流控与平台资格流程移除的回归。
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.site.contract import SiteSnapshot


@pytest.fixture
def catalog():
    """
    直接加载源码目录，避免全仓测试的空站点占位掩盖真实行为。
    """
    source = Path(__file__).parents[1] / "app/application/site/sites.py"
    assert source.is_file(), "站点目录必须由可审查的 Python 源码实现"
    spec = importlib.util.spec_from_file_location("source_site_catalog_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_builtin_rules_are_readable_without_platform_account(catalog):
    """
    未提供平台资格凭据也能读取完整站点解析规则。
    """
    helper = catalog.SitesHelper()
    assert len(helper.get_indexsites()) == 198
    rule = helper.get_indexer("m-team.io")
    assert rule["search"]
    assert rule["torrents"]
    assert helper.get_authsites() == {}
    assert helper.check_user()[0] is False


def test_configured_site_keeps_credentials_and_schema(catalog):
    """
    真实站点登录凭据和解析规则在同步与异步查询中一起返回。
    """
    import asyncio

    site = SiteSnapshot(id=7, name="测试站点", domain="m-team.io",
                        url="https://xp.m-team.io/", cookie="test-cookie",
                        apikey="test-key", token="test-token", is_active=True)

    async def async_list():
        return [site]

    async def async_get(_domain):
        return site

    repository = SimpleNamespace(list_active=lambda: [site], get_by_domain=lambda domain: site,
                                 async_list_active=async_list, async_get_by_domain=async_get)
    helper = catalog.SitesHelper(repository=repository)
    values = [helper.get_indexers()[0], helper.get_indexer("m-team.io"),
              asyncio.run(helper.async_get_indexers())[0],
              asyncio.run(helper.async_get_indexer("m-team.io"))]
    for value in values:
        assert value["id"] == 7
        assert value["cookie"] == "test-cookie"
        assert value["apikey"] == "test-key"
        assert value["token"] == "test-token"
        assert value["search"]
        assert value["torrents"]


def test_returned_rules_do_not_share_mutable_state(catalog):
    """
    调用者修改一次查询结果不能改变后续解析规则。
    """
    helper = catalog.SitesHelper()
    first = helper.get_indexer("m-team.io")
    first["search"].clear()
    assert helper.get_indexer("m-team.io")["search"]


def test_unknown_and_deceptive_domains_are_not_matched(catalog):
    """
    未知域名和伪装后缀不能误匹配已支持站点。
    """
    helper = catalog.SitesHelper()
    assert helper.get_indexer("unsupported.example") is None
    assert helper.get_indexer("m-team.io.evil.example") is None
    assert helper.get_indexer("https://xp.m-team.io/")["search"]


def test_custom_rule_is_process_local_and_copied(catalog):
    """
    保留插件添加自定义规则的能力，同时隔离可变输入。
    """
    helper = catalog.SitesHelper()
    rule = {"name": "自定义", "domain": "https://custom.example/", "search": {"params": {}}}
    assert helper.add_indexer("custom.example", rule)
    rule["search"].clear()
    assert helper.get_indexer("custom.example")["search"] == {"params": {}}


def test_rate_limit_counts_only_accepted_requests(catalog):
    """
    被流控拒绝的请求不消耗后续配额，不同域名互不影响。
    """
    now = [0.0]
    limiter = catalog.SiteRateLimiter(clock=lambda: now[0])
    assert limiter.check_rate_limit("first", 10, 2, 1)[0]
    now[0] = 0.5
    assert not limiter.check_rate_limit("first", 10, 2, 1)[0]
    assert limiter.check_rate_limit("second", 10, 2, 1)[0]
    now[0] = 1.0
    assert limiter.check_rate_limit("first", 10, 2, 1)[0]
    now[0] = 2.0
    assert not limiter.check_rate_limit("first", 10, 2, 1)[0]
    now[0] = 10.0
    assert limiter.check_rate_limit("first", 10, 2, 1)[0]


def test_platform_auth_api_is_removed():
    """
    站点路由不再提供平台资格列表或认证提交接口。
    """
    from app.api.endpoints.site import router

    assert all(route.path != "/auth" for route in router.routes)


def test_platform_auth_scheduler_is_removed():
    """
    后台没有遗留的平台资格认证任务实现。
    """
    from app.scheduler.reconcile import SchedulerReconcileOwner

    assert not hasattr(SchedulerReconcileOwner, "user_auth")


def test_every_shipped_rule_is_reachable_without_authentication(catalog):
    """
    所有已交付的站点规则都可按规范域名读取，不因源码匹配丢失站点。
    """
    import json

    rules = json.loads(Path(catalog.__file__).with_name("sites.json").read_text())["sites"]
    helper = catalog.SitesHelper()
    for domain, expected in rules.items():
        assert helper.get_indexer(domain) == expected, domain


def test_retired_platform_auth_operations_are_not_advertised():
    """
    MCP 和接口目录不能继续暴露已删除的平台资格认证操作。
    """
    from app.agent.policy.api import API_OPERATION_ROUTES, API_OPERATION_SPECS

    retired = {"site.auth.options", "site.authenticate"}
    assert retired.isdisjoint(API_OPERATION_ROUTES)
    assert retired.isdisjoint(spec.operation_id for spec in API_OPERATION_SPECS)
