# 本地站点模式（实验版）

本 fork 提供不依赖指定 PT 平台账号的站点索引模式。它只移除 MoviePilot 自身的平台资格门槛；资源站点需要的 Cookie、API Key、Token、下载权限与配额仍由资源站点校验。管理员登录、API Key、MFA、插件独立授权保留。

## 支持范围与实现

- 主程序基线：`v3.1.9`，提交 `811503b709f6207ee12ce17fd3a1a113effee723`。
- Linux x86-64，标准 CPython 3.14，认证资源 `3.0.5`。
- 官方基础镜像固定为 `jxxghp/moviepilot-v3@sha256:1d10be0338d3d93669feac716ead9a094dc06c433fcb8a0ff271f2e07345e341`。
- 认证扩展原 SHA-256：`ebd2067fb72e5fc1474a5b13da69367c269705bb1b1c0c7499c90de2460e8aef`。
- 本地模式 SHA-256：`0c5344605be811517e4a18239a1d6def27912b7d520cb74b635c44763ed42f7c`。

官方资源位于 `MoviePilot-Resources/resources.v3/`，闭源 Cython 扩展同时提供认证和站点索引。反汇编定位到内部认证类的 `get_auth_level`。实验补丁只替换该函数入口的 15 字节，保留 ENDBR64，并通过原动态链接 GOT 槽尾调用 `PyLong_FromLong(2)`，返回普通站点能力所需等级。

这没有恢复二进制的全部源码，也没有修改站点认证协议。主程序启动认证和定时认证在等级达到 2 时跳过；索引接口继续使用原有解析规则与用户配置。界面仍可能显示上游的认证状态文案和手动认证入口，本地模式不表示任何 PT 站点验证过你的账号。

资源版本或 Python ABI 变化时拒绝应用补丁。镜像关闭资源和程序自动更新；手动安装资源或覆盖扩展也会撤销本地模式。升级应先核实新资源、重新分析和验收，不能复用旧偏移。

## 构建

```bash
docker build -f docker/Dockerfile.local-site-mode -t moviepilot:3.1.9-local-site .
```

无需提供真实账号即可构建。构建保留 `.so.upstream` 副本，原始镜像也保持不变。

## 隔离测试部署

使用全新配置目录，不挂载已有 MP 配置、下载目录或媒体库：

```bash
mkdir -p ./local-site-test-config
docker run -d --name moviepilot-local-site-test \
  -p 127.0.0.1:13000:3000 -p 127.0.0.1:13001:3001 \
  -v "$PWD/local-site-test-config:/config" \
  -e AUTO_UPDATE_RESOURCE=false -e MOVIEPILOT_AUTO_UPDATE=false \
  moviepilot:3.1.9-local-site
```

首次访问仍需创建管理员和 API Key。部署到远程主机时，可用 SSH 转发前端测试端口：

```bash
ssh -N -L 13000:127.0.0.1:13000 volc
```

然后打开 `http://127.0.0.1:13000`。配置真实资源站点时仍需填写相应 Cookie 或 Token。

## 验证

`tests/test_local_site_mode_patch.py` 检查版本拒绝、输出保护和真实样本的精确变更范围。真实样本通过只读挂载 `/local-site-upstream.so` 提供，不入库。

`scripts/verify_local_site_mode.py` 必须在真实镜像里运行，不能使用 pytest 的站点扩展占位。容器关闭网络，并用模拟站点替代数据库查询，检验同步与异步索引、凭据透传及等级 99 插件仍被拒绝。原版运行时不加 `--local`，修改版添加 `--local`。

```bash
docker run --rm --network none --entrypoint /opt/venv/bin/python \
  -e CONFIG_DIR=/tmp/mp-local-mode-check -e PYTHONPATH=/app \
  -v "$PWD/scripts/verify_local_site_mode.py:/verify.py:ro" \
  moviepilot:3.1.9-local-site /verify.py --local
```

真实站点搜索与实际下载需要独立账号和资源授权，离线验收不代表这些外部操作成功。

## 本次验收记录

在 volc 的独立 x86-64 测试实例上完成以下检查：

- 补丁保护与站点认证、API 授权、登录限流、MFA 相关回归：108 项通过。
- 三个新增 Python 文件的 pylint 检查通过。
- 关闭网络并加载真实扩展：原版等级 1，同步、异步索引均为空；本地模式等级 2，同步、异步索引均返回模拟站点。
- Cookie、API Key、Token 透传保持不变；等级 99 插件仍执行独立密钥校验并拒绝虚构密钥。
- 测试实例健康检查及前端均返回 HTTP 200；正确管理员登录返回等级 2；错误密码、无登录凭据和错误 API Key 返回 401。
- 浏览器可打开 v3.1.9 登录页。

独立审查覆盖初始候选，指出的验收脚本缺参问题已修正，并通过真实扩展验收验证。未执行整仓测试；本次没有修改产品 Python 源码、依赖或数据库结构。真实站点搜索和实际下载未验收，测试实例也观察到 TMDB 网络连接失败，这些外部能力不属于以上通过结论。

## 回退

停止并移除本测试容器，保留测试配置目录，再使用原始官方镜像创建测试实例即可恢复原认证行为。不要将实验配置覆盖到生产配置；不需要修改或迁移已有数据库。
