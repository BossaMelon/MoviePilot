"""
提供需要登录的 Peach 番号、下载和馆藏接口；写操作要求管理权限。
"""

from typing import Optional

from fastapi import Depends, HTTPException

from app.api.dependencies.auth import get_current_active_manage_user, get_current_active_user
from app.api.response import ResponseAPIRouter
from app.application.peach.service import PeachBridgeService, normalize_code
from app.runtime.settings import get_runtime_setting
from app.schemas.peach import (
    PeachDownloadRequest,
    PeachLibraryData,
    PeachSearchData,
    PeachSearchRequest,
    PeachStatusData,
    PeachTask,
    PeachTasksData,
)
from app.schemas.response import Response
from app.startup.composition.peach import build_peach_bridge

router = ResponseAPIRouter()


def get_peach_bridge() -> PeachBridgeService:
    """
    提供可在隔离测试中覆盖的桥接服务依赖。
    """
    try:
        return build_peach_bridge()
    except ValueError as error:
        raise HTTPException(status_code=503, detail="Peach 桥接配置不可用") from error


@router.get("/status", response_model=Response[PeachStatusData], dependencies=[Depends(get_current_active_user)])
def status() -> Response[PeachStatusData]:
    """
    返回配置与连接状态，不返回服务地址、令牌或磁盘路径。
    """
    configured = all(get_runtime_setting(key) for key in ("PEACH_URL", "PEACH_TOKEN", "PEACH_DOWNLOAD_ROOT"))
    if not configured:
        return Response(success=True, data=PeachStatusData(configured=False, message="Peach 桥接尚未配置"))
    try:
        service = build_peach_bridge()
        connected = service.port.connected()
        return Response(success=True, data=PeachStatusData(configured=True, connected=connected,
                        message="" if connected else "Peach 导入配置尚未就绪"))
    except Exception:
        return Response(success=True, data=PeachStatusData(configured=True, message="Peach 暂不可用，请检查专用连接配置"))


@router.post("/search", response_model=Response[PeachSearchData], dependencies=[Depends(get_current_active_manage_user)])
def search(request: PeachSearchRequest, service: PeachBridgeService = Depends(get_peach_bridge)) -> Response[PeachSearchData]:
    """
    按番号检索站点并返回不含凭据的候选。
    """
    try:
        code, results = service.search(request.code, request.sites)
        return Response(success=True, data=PeachSearchData(code=code, results=results))
    except ValueError:
        return Response(success=False, message="番号格式或站点配置无效")
    except Exception:
        return Response(success=False, message="站点搜索暂不可用")


@router.post("/downloads", response_model=Response[PeachTask], dependencies=[Depends(get_current_active_manage_user)])
def download(request: PeachDownloadRequest, service: PeachBridgeService = Depends(get_peach_bridge)) -> Response[PeachTask]:
    """
    仅接受服务端生成的候选标识，不接受资源 URL 或站点凭据。
    """
    try:
        return Response(success=True, data=service.download(request.candidate_id, request.downloader))
    except ValueError:
        return Response(success=False, message="候选失效或下载配置无效，请重新搜索")


@router.get("/tasks", response_model=Response[PeachTasksData], dependencies=[Depends(get_current_active_user)])
def tasks(service: PeachBridgeService = Depends(get_peach_bridge)) -> Response[PeachTasksData]:
    """
    只读桥接任务状态。
    """
    return Response(success=True, data=PeachTasksData(tasks=service.tasks()))


@router.post("/sync", response_model=Response[PeachTasksData], dependencies=[Depends(get_current_active_manage_user)])
def sync(service: PeachBridgeService = Depends(get_peach_bridge)) -> Response[PeachTasksData]:
    """
    核实下载完成后驱动幂等入库，失败保留状态供下次重试。
    """
    return Response(success=True, data=PeachTasksData(tasks=service.sync()))


@router.get("/library", response_model=Response[PeachLibraryData], dependencies=[Depends(get_current_active_user)])
def library(code: Optional[str] = None, service: PeachBridgeService = Depends(get_peach_bridge)) -> Response[PeachLibraryData]:
    """
    查询 Peach 馆藏，只返回安全摘要。
    """
    try:
        return Response(success=True, data=service.port.library(normalize_code(code) if code else None))
    except ValueError:
        return Response(success=False, message="番号格式或馆藏响应无效")
    except Exception:
        return Response(success=False, message="Peach 馆藏暂不可用")
