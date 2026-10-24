"""
验证平台资格认证流程已移除，普通站点能力不依赖指定平台账号。
"""
from app.api.endpoints.site import router
from app.scheduler.reconcile import SchedulerReconcileOwner
from app.startup.initializers import modules


def test_platform_auth_routes_are_absent():
    """
    不再公开认证提供方或资格认证提交接口。
    """
    assert all(route.path != "/auth" for route in router.routes)


def test_startup_does_not_offer_platform_auth():
    """
    启动器不再有认证请求或认证失败提示步骤。
    """
    assert not hasattr(modules, "user_auth")
    assert not hasattr(modules, "check_auth")


def test_scheduler_has_no_platform_auth_task():
    """
    后台没有可调用的平台资格认证任务。
    """
    assert not hasattr(SchedulerReconcileOwner, "user_auth")
