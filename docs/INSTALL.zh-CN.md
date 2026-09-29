# Gateway / 工作台安装

## 前置条件

- 控制端：Python 3.11+（使用标准库 `tomllib`）、Pillow、OpenSSH 客户端；视频/音频处理需要 `ffmpeg` / `ffprobe`。
- 当前 `DGXGateway/manage.sh` 使用 macOS `launchctl`；可在 Linux 前台运行 Python 入口，但需要自行编写服务管理，不能运行 launchd 命令。
- DGX 节点：驱动、Docker、NVIDIA Container Toolkit 与目标运行时兼容；ARM64/GB10 的镜像必须匹配，不能用普通 x86 镜像代替。
- 四机 GPU 互联单独验证。管理网、模型通信网络和外部下载网络分别配置。不要从旧两机网络示例推断四机拓扑。

## 1. 本地 Python 环境

```bash
git clone https://github.com/DennyWanye/DGXGateway.git
cd DGXGateway
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

控制面大部分使用标准库；GPU 库属于各模型容器，不安装到控制端 Python。需要切图、ASR或额外媒体操作时，按对应运行时依赖单独部署。

## 2. 配置

复制 `DGXGateway/config/gateway.example.toml` 至自己选择的私有目录，逐项配置：

| 配置 | 要填写的内容 |
|---|---|
| gateway.bind / allowed_origins | 实际监听地址和浏览器来源；默认127.0.0.1 |
| gateway.lan_no_auth | 默认false；开启会让可访问服务的人直接使用LAN接口 |
| state_dir / admin_token_file | 私有状态目录与随机管理令牌文件，目录0700、文件0600 |
| cluster.repo_root / lock_cli | 本机仓库和锁脚本的绝对路径 |
| authority | 本机authority记录/credential路径，部署时生成，不能拷贝别人的值 |
| nodes | 自己的hostname、地址、SSH用户；核验host key并启用BatchMode |
| authority.mutation_enabled | 默认false；仅在authority和节点控制入口配置完成后启用 |
| models.* | 各模型已安装的路径、端口、token文件与运行时入口 |

示例里的 `/Users/operator`、`/home/operator`、`dgx-head` 等不是自动发现结果；`192.0.2.0/24` 等是文档用地址，必须改成自己的配置。部分旧适配器与脚本还有站点常量，安装对应模型前搜索并修改，不要只改一份TOML就假设全部生效。

## 3. 初始化与启动

先完成配置和本地虚拟环境。`manage.sh init` 包含 authority 初始化，会使用配置中的路径；它不启动 GPU 模型。

```bash
export DGX_GATEWAY_CONFIG="$HOME/.config/dgx-cluster-gateway/gateway.toml"
export DGX_GATEWAY_READY_URL='http://127.0.0.1:18082/api/v1/snapshot'
DGXGateway/manage.sh init
.venv/bin/python -m DGXGateway.dgx_gateway --config "$DGX_GATEWAY_CONFIG"
```

上面前台运行适合首次调试。`run-gateway.sh` 和 `manage.sh` 的后台路径调用 `python3`；使用 launchd 前应把它们调整为虚拟环境 Python 的绝对路径。不要假设交互式激活的venv会自动传给launchd。默认只读配置不允许模型切换；只有明确完成权限和运行时部署才启用mutation。

访问 `http://127.0.0.1:18082/`，MCP为 `/mcp`。先请求状态，确认未知/停止/健康的真实差异，再执行任何模型生命周期动作。读取网页本身不启动模型。

## 4. 创作工作台

已有构建产物位于 `ComfyUI/custom_nodes/dgx_remote_h3/web/studio`，React源码在相邻 `frontend`。

```bash
cd ComfyUI/custom_nodes/dgx_remote_h3/frontend
npm ci
npm run build
```

`package-lock.json` 保留依赖版本。构建目录由Vite写入 `../web/studio`。工作台需要drama后端；`scripts/run-drama-preview.py` 是独立开发入口，运行前用 `--help` 查看参数并使用新的私有state目录。不要把现场数据库复制进公开仓库。工作台业务MCP与模型MCP的工具集不同。

## 5. 模型安装与验证

按 [模型总览](models/README.zh-CN.md) 选择对应指南。控制面可以安装成功而模型仍未部署。每个模型至少核验：下载摘要、容器版本、节点身份、实际加载、短请求、资源释放与失败恢复。GPU启动/切换通过Gateway；随包的独立诊断脚本不能用来旁路正在运行的控制面。

发布检查只覆盖公开副本的静态结构、脱敏和部分定向本地检查。没有为了发布重启现有Gateway、切换模型、改网络或执行四机回归。
