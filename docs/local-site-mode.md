# 无平台资格认证的源码站点模式

本 fork 完整移除指定 PT 平台资格认证步骤。站点索引逻辑使用 Python，198 个站点的搜索、分类、字幕和字段解析规则使用 JSON；不再依赖站点原生扩展、加密数据包或机器码偏移补丁。

管理员登录、API Key、MFA、资源站点实际 Cookie/API Key/Token、下载权限、配额和插件独立密钥授权保持有效。无需先拥有某个指定平台的账号才能添加、搜索自己有权使用的资源站点。

## 已删除的流程

- 头像菜单的平台认证入口、认证弹窗和认证参数提交。
- `GET/POST /api/v1/site/auth` 和对应 MCP 操作。
- 首次安装向导中的平台认证问题与隐藏认证 CLI。
- 启动资格认证、认证失败提示、后台 `user_auth` 定时任务。
- 新增站点时的平台资格检查。
- 按系统/Python ABI 下载站点二进制、升级继承旧扩展的流程。

旧数据库里的平台认证参数不做破坏性删除，也不再读取、执行或提供写入口。为旧插件保留的 `auth_level=2` 是普通能力等级声明；`get_authsites()` 返回空目录，`check_user()` 明确返回“流程已移除”，不会发请求或伪造平台认证成功。插件独立密钥仍由原策略验证。

## 源码与数据

- `app/application/site/sites.py`：目录读取、用户配置合并、域名匹配、同步/异步索引和流控。
- `app/application/site/sites.json`：可审查的站点解析规则，当前数据版本 `3.0.22`，保留来源提交记录。
- 数据由官方 v3.1.9 镜像原有规则导出，不包含用户账号、Cookie 或 Token；已有解析器和各站点实际访问协议继续使用。
- `SiteQueryPort` 由启动组合根注入，源码目录不越层导入数据库或联网认证。

主程序基于 `v3.1.9`（`811503b709f6207ee12ce17fd3a1a113effee723`）；前端基于同版本 `795c975850289ac2e0146b18c05cf63541cb352a`。此前 `bc088b80` 的临时偏移补丁已由源码实现替换，当前文件树不包含补丁工具。

## 构建与安装

前后端源码分开维护：

- [后端](https://github.com/BossaMelon/MoviePilot/tree/feature/local-site-mode)
- [前端](https://github.com/BossaMelon/MoviePilot-Frontend/tree/feature/local-site-mode)

在前端使用 Node 24 和冻结的 yarn.lock 安装依赖后执行 `yarn build`，将 `dist/` 的内容复制到后端 `frontend-dist/`，再执行：

```bash
docker build -f docker/Dockerfile.local-site-mode -t moviepilot:3.1.9-source-site .
```

该构建固定已验证的官方基础运行时，删除旧站点扩展，覆盖当前后端源码和前端构建结果。站点 Python 与 JSON 随应用版本交付。源码安装不再下载站点二进制，发现遗留扩展会在安装事务中移除，失败时回滚；不删除其他 Rust 或 SQLite 原生依赖。

独立站点资源更新已停用。程序和前端更新源指向本 fork，不会静默回退上游；fork 尚无对应 Release 时在线更新会明确失败。测试实例保持自动更新关闭，升级请重新构建本分支或发布经过验证的 fork 版本。

## 隔离部署与代理

使用独立测试配置目录，不挂载真实媒体：

```bash
mkdir -p ./source-site-test-config
docker run -d --name moviepilot-local-site-test \
  -p 127.0.0.1:13000:3000 -p 127.0.0.1:13001:3001 \
  -v "$PWD/source-site-test-config:/config" \
  -e AUTO_UPDATE_RESOURCE=false -e MOVIEPILOT_AUTO_UPDATE=false \
  moviepilot:3.1.9-source-site
```

远程查看可使用 `ssh -N -L 13000:127.0.0.1:13000 volc`，再访问 `http://127.0.0.1:13000`。首次安装仍需设置管理员与 API Key。

测试主机已有代理只监听主机回环地址，容器里的 `127.0.0.1` 不能直接访问它。本次以独立转发服务连接既有代理，只绑定 Docker bridge 网关地址，没有更改已有代理或防火墙。测试实例设置 `PROXY_HOST`、`HTTP_PROXY`、`HTTPS_PROXY` 为该转发地址，`NO_PROXY` 包含本机和 bridge 网关。具体地址保存在测试部署目录，不作为通用默认值写入产品。

## 验证范围

- 源码目录测试覆盖全部 198 条规则、域名误匹配、配置与凭据透传、同步/异步查询、可变结果隔离和流控。
- API、安装向导、MCP 与调度测试验证平台资格步骤确实删除。
- 登录/API/MFA 与插件独立授权的相关回归已通过；测试实例正确登录 200、错误密码与未登录站点访问 401。
- 构建实例检查加载的是 `sites.py`，且没有残留站点原生扩展或加密资源。
- 真实站点搜索与下载需要相应账号和操作授权；未执行这些外部操作时不声称已验证。

测试实例已确认加载 `/app/app/application/site/sites.py`，目录内无站点 `.so/.pyd/.bin`，可读规则共 198 条。应用自身网络检测中 TMDB API、备用域名与网站均成功，经独立桥接转发使用主机既有代理。前端构建、类型检查、菜单回归及后端本次相关回归均通过。

完整测试仍有原版 v3.1.9 同环境可复现的音乐文件变更检测、插件扫描与 shell 环境失败；没有为使测试变绿修改这些无关断言，不能宣称完整套件全绿。浏览器确认登录页可加载；登录后菜单由组件回归验证，未将远端凭据传到本机。

## 回退

旧源码提交和旧测试镜像保留为恢复点。停止本任务测试容器，使用之前镜像与相同测试配置创建实例即可回退。没有数据库结构迁移，不修改真实媒体或其他服务。
