"""
源码站点索引目录、配置合并和进程内访问流控。

解析规则来自同目录的可审查 JSON；不加载扩展、pickle、加密资源或平台资格认证。
数据库通过 SiteQueryPort 注入，目录本身不会连接外部服务。
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import fields
from functools import lru_cache
from pathlib import Path

from app.application.site.contract import SiteQueryPort, SiteSnapshot
from app.domain.site import extract_domain
from app.schemas.common import JsonData

_repository: SiteQueryPort | None = None
_catalog_lock = threading.RLock()
_custom_rules: dict[str, dict[str, JsonData]] = {}
_RULES_PATH = Path(__file__).with_name("sites.json")


def configure_site_repository(repository: SiteQueryPort) -> None:
    """
    由启动组合根绑定当前应用的站点查询端口。
    """
    global _repository
    _repository = repository


def reset_site_catalog() -> None:
    """
    关闭当前应用时撤销仓储、插件规则和流控状态。
    """
    global _repository
    _repository = None
    with _catalog_lock:
        _custom_rules.clear()
    _limiter.clear()
    _load_catalog.cache_clear()


@lru_cache(maxsize=4)
def _load_catalog(path: Path) -> tuple[str, dict[str, dict[str, JsonData]]]:
    """
    加载声明式目录；格式错误时明确失败，不回退旧二进制。
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("version"), str):
        raise ValueError("站点目录缺少版本")
    rules = document.get("sites")
    if not isinstance(rules, dict) or not rules:
        raise ValueError("站点目录缺少索引规则")
    for domain, rule in rules.items():
        if not isinstance(domain, str) or not isinstance(rule, dict):
            raise ValueError("站点索引格式无效")
        if not isinstance(rule.get("name"), str) or not isinstance(rule.get("domain"), str):
            raise ValueError(f"站点 {domain} 缺少名称或地址")
    return document["version"], rules


def _plain_value(value: JsonData) -> JsonData:
    """
    将冻结快照内的 JSON 容器转换为与调用者隔离的普通容器。
    """
    if isinstance(value, Mapping):
        return {key: _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    return value


class SiteRateLimiter:
    """
    用单调时钟和锁维护每个站点的滑动窗口及最小请求间隔。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """
        注入秒级单调时钟；只有已放行的请求计入窗口。
        """
        self._clock = clock
        self._lock = threading.Lock()
        self._history: dict[str, deque[float]] = {}

    def clear(self) -> None:
        """
        清空当前应用生命周期的流控记录。
        """
        with self._lock:
            self._history.clear()

    def check_rate_limit(self, domain: str, limit_interval: int = 0,
                         limit_count: int = 0, limit_seconds: int = 0) -> tuple[bool, str]:
        """
        同时检查统计窗口次数和相邻间隔，拒绝时不扣配额。

        :param domain: 共享流控的规范站点域名
        :param limit_interval: 统计窗口长度，单位为秒，零表示关闭窗口限制
        :param limit_count: 窗口内最多放行次数，零表示关闭次数限制
        :param limit_seconds: 相邻请求最小间隔，单位为秒
        :return: 是否放行及拒绝原因
        """
        if limit_interval < 0 or limit_count < 0 or limit_seconds < 0:
            return False, "站点流控参数不能为负数"
        window_enabled = limit_interval > 0 and limit_count > 0
        if not window_enabled and limit_seconds == 0:
            return True, ""
        now = self._clock()
        with self._lock:
            history = self._history.setdefault(domain, deque())
            if history and now - history[-1] < limit_seconds:
                return False, f"触发流控规则，访问间隔不得小于 {limit_seconds} 秒"
            if window_enabled:
                while history and now - history[0] >= limit_interval:
                    history.popleft()
                if len(history) >= limit_count:
                    return False, f"触发流控规则，{limit_interval} 秒内最多访问 {limit_count} 次"
            else:
                history.clear()
            history.append(now)
        return True, ""


_limiter = SiteRateLimiter()


class SitesHelper:
    """
    为宿主和旧插件提供同步及异步站点索引接口。

    平台资格认证已移除；auth_level 仅保留普通能力等级的插件兼容读接口。
    """

    def __init__(self, repository: SiteQueryPort | None = None) -> None:
        """
        可直接注入测试仓储，否则读取启动组合根绑定的仓储。
        """
        self._repository_override = repository

    @property
    def auth_level(self) -> int:
        """
        返回旧插件协议的普通能力等级，不代表任何平台认证结果。
        """
        return 2

    @property
    def auth_version(self) -> str:
        """
        不再提供平台认证资源版本。
        """
        return ""

    @property
    def indexer_version(self) -> str:
        """
        返回随源码发布的 JSON 目录版本。
        """
        return _load_catalog(_RULES_PATH)[0]

    def _repository(self) -> SiteQueryPort | None:
        return self._repository_override or _repository

    @staticmethod
    def _rule(domain: str) -> dict[str, JsonData] | None:
        """
        按完整域名或注册域名匹配，拒绝任意字符串后缀匹配。
        """
        key = extract_domain(domain)
        if not key:
            return None
        with _catalog_lock:
            custom = _custom_rules.get(key)
            if custom is not None:
                return deepcopy(custom)
        rules = _load_catalog(_RULES_PATH)[1]
        if key in rules:
            return deepcopy(rules[key])
        for rule in rules.values():
            aliases = rule.get("ext_domains") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            if not isinstance(aliases, list):
                continue
            if any(extract_domain(alias) == key for alias in aliases if isinstance(alias, str)):
                return deepcopy(rule)
        return None

    @staticmethod
    def _merge(rule: dict[str, JsonData], site: SiteSnapshot) -> dict[str, JsonData]:
        """
        合并已配置站点的真实访问地址和登录参数，不修改共享模板。
        """
        values = {field.name: _plain_value(getattr(site, field.name)) for field in fields(SiteSnapshot)}
        if values["public"] is None:
            values.pop("public")
        rule.update(values)
        rule["domain"] = site.url
        return rule

    def get_indexers(self) -> list[dict[str, JsonData]]:
        """
        返回已启用且有解析规则的站点，真实登录凭据按配置透传。
        """
        repository = self._repository()
        if repository is None:
            return []
        return [self._merge(rule, site) for site in repository.list_active()
                if site.domain and (rule := self._rule(site.domain)) is not None]

    async def async_get_indexers(self) -> list[dict[str, JsonData]]:
        """
        使用异步仓储读取已启用站点，避免在事件循环同步访问数据库。
        """
        repository = self._repository()
        if repository is None:
            return []
        sites = await repository.async_list_active()
        return [self._merge(rule, site) for site in sites
                if site.domain and (rule := self._rule(site.domain)) is not None]

    def get_indexer(self, domain: str) -> dict[str, JsonData] | None:
        """
        返回域名对应的解析规则，并合并已经保存的站点配置。
        """
        rule = self._rule(domain)
        repository = self._repository()
        if rule is not None and repository is not None:
            site = repository.get_by_domain(extract_domain(domain))
            if site is not None:
                rule = self._merge(rule, site)
        return rule

    async def async_get_indexer(self, domain: str) -> dict[str, JsonData] | None:
        """
        异步合并域名对应的已保存配置，未配置时仍可读取内置规则。
        """
        rule = self._rule(domain)
        repository = self._repository()
        if rule is not None and repository is not None:
            site = await repository.async_get_by_domain(extract_domain(domain))
            if site is not None:
                rule = self._merge(rule, site)
        return rule

    def get_indexsites(self) -> dict[str, JsonData]:
        """
        返回可添加站点的公开名称和地址，不包含任何用户凭据。
        """
        return {domain: {"id": rule.get("id"), "name": rule["name"],
                         "url": rule["domain"], "public": bool(rule.get("public"))}
                for domain, rule in _load_catalog(_RULES_PATH)[1].items()}

    def add_indexer(self, domain: str, indexer: dict[str, JsonData]) -> bool:
        """
        保留旧插件添加进程内自定义解析规则的入口，不写入程序源码。
        """
        key = extract_domain(domain)
        if not key or not isinstance(indexer, dict) or not indexer.get("name") or not indexer.get("domain"):
            return False
        with _catalog_lock:
            _custom_rules[key] = deepcopy(indexer)
        return True

    def check(self, domain: str) -> tuple[bool, str]:
        """
        按用户站点配置执行流控，不检查任何平台资格。
        """
        repository = self._repository()
        site = repository.get_by_domain(extract_domain(domain)) if repository is not None else None
        if site is None:
            return True, ""
        return _limiter.check_rate_limit(extract_domain(domain), site.limit_interval or 0,
                                         site.limit_count or 0, site.limit_seconds or 0)

    @staticmethod
    def get_authsites() -> dict[str, JsonData]:
        """
        旧插件兼容入口：平台资格认证已移除，没有可认证平台。
        """
        return {}

    @staticmethod
    def check_user(*_args: object, **_kwargs: object) -> tuple[bool, str]:
        """
        旧插件兼容入口：明确声明流程已移除，不联网或伪造认证成功。
        """
        return False, "平台资格认证已移除，无需执行此操作"
