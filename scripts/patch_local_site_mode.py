"""
为固定版本的 Linux x86-64 站点扩展生成本地模式副本。

只改变平台资格等级查询，不改变站点 Cookie、API 密钥、管理员登录或插件独立授权。
输入必须与已分析的完整资源哈希一致；原文件始终保留，目标文件不得覆盖已有内容。
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil

# 官方 v3.1.9 镜像中的 CPython 3.14、认证资源 3.0.5；不是通用偏移补丁。
UPSTREAM_SHA256 = "ebd2067fb72e5fc1474a5b13da69367c269705bb1b1c0c7499c90de2460e8aef"
LOCAL_SHA256 = "0c5344605be811517e4a18239a1d6def27912b7d520cb74b635c44763ed42f7c"
LEVEL_ENTRY_OFFSET = 0x1D860
ORIGINAL_ENTRY = bytes.fromhex("f30f1efa4157415641554154554889")
# 保留 ENDBR64；按 SysV ABI 将 2 传给原有 PyLong_FromLong 导入，尾调用返回 Python 整数。
# 通过原 GOT 槽调用，保留动态装载器处理 ASLR 与惰性绑定的行为。
LOCAL_ENTRY = bytes.fromhex("f30f1efabf02000000ff25c1590600")


def patch_bytes(data: bytes) -> bytes:
    """
    生成仅修改认证等级查询入口的资源副本。

    :param data: 完整的官方资源或本工具生成的资源
    :return: 本地模式资源；已修改的输入保持幂等
    :raises ValueError: 资源版本或修改结果与已分析样本不符
    """
    digest = hashlib.sha256(data).hexdigest()
    if digest == LOCAL_SHA256:
        return data
    if digest != UPSTREAM_SHA256:
        raise ValueError(f"不支持的站点扩展 SHA-256：{digest}；拒绝修改")
    end = LEVEL_ENTRY_OFFSET + len(ORIGINAL_ENTRY)
    if data[LEVEL_ENTRY_OFFSET:end] != ORIGINAL_ENTRY:
        raise ValueError("认证查询入口与已分析样本不一致；拒绝修改")
    result = data[:LEVEL_ENTRY_OFFSET] + LOCAL_ENTRY + data[end:]
    if hashlib.sha256(result).hexdigest() != LOCAL_SHA256:
        raise ValueError("修改后的完整资源校验失败；拒绝写入")
    return result


def write_patch(source: Path, destination: Path) -> str:
    """
    校验源文件后写入新文件，保留源文件和已有目标。

    :param source: 原认证扩展路径
    :param destination: 尚不存在的输出路径
    :return: 输出文件 SHA-256
    :raises ValueError: 原地修改或输入资源不受支持
    :raises FileExistsError: 目标已经存在，包括符号链接
    """
    if source.resolve() == destination.resolve():
        raise ValueError("请指定不同的输出路径，保留原认证扩展")
    result = patch_bytes(source.read_bytes())
    with destination.open("xb") as output:
        output.write(result)
    shutil.copymode(source, destination)
    return hashlib.sha256(result).hexdigest()


def main() -> None:
    """
    从命令行生成版本受限的本地模式扩展副本。
    """
    parser = argparse.ArgumentParser(description="生成 MoviePilot v3.1.9 本地站点模式扩展")
    parser.add_argument("source", type=Path, help="官方认证扩展")
    parser.add_argument("destination", type=Path, help="尚不存在的输出文件")
    args = parser.parse_args()
    try:
        digest = write_patch(args.source, args.destination)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(f"本地模式扩展已生成：{args.destination} SHA-256={digest}")


if __name__ == "__main__":
    main()
