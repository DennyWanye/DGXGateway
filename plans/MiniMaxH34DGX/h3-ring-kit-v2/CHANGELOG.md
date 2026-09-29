# V2 变更摘要

日期：2026-09-18。基于原 Ring4 实验包和“先跑通、再好用”补充。

## 保持不变

4台Spark协同生成一个视频；TP1/U1/Ring4、FA、encoder replicate、eager；原源码SHA；现网与官方回退；先功能后性能；整个组单活跃任务。原内核与通信诊断代码保留。

- 容器内用探测用 `MiniMaxH3Pipeline` 根索引覆盖官方 `MiniMaxH3ModularPipeline` 根文件：SGLang 会在拼接 `--model-variant` 分区前校验根 `model_index.json`，官方 3 元组分规格会要求根目录存在 `transformer/` 与 `vae/`。磁盘上的官方文件不变。
- serve 显式 `--pipeline-class-name MiniMaxH3Pipeline`，避免根索引类名把 AUTO 后端落到 Diffusers。
- docker `--memory/--memory-swap 104g`：GB10 统一内存上 FL2VA 编码器 48Gi + DiT 62Gi 会把 sshd 饿死；cgroup 限额让容器 OOM，而不是整机失去 SSH。

## 新增

- 四个受控Profile：FL2VA官方来源、Ref2VA官方来源、Ref2VA+BF16 Heretic、Hybrid实验。
- JSON配置迁移与模型／镜像／Profile指纹检查，四节点独立回执合并。
- 固定revision的按资产下载、上游哈希验证、同内容官方组件本地复制、跨节点SHA256回执。
- Heretic配置归一核对和官方H3 tokenizer检查，不把配置通过当运行通过。
- 同路径本地素材、任务匹配与路径边界校验、请求构造和提示词模板。
- S0 SQLite串行队列、素材hash、未知POST阻塞、已知job续查和技术解码检查。
- 切换事务、独立实验镜像、P1/P2 A/B、手工质量和实际使用验收。

## 与原指令的重要区别

- 日常serve改用profile_node.py；原run_node.sh保留给kernel/comm/help，不混用两套serve。
- 未验收的新Profile默认placement=auto；已实测通过的speed应显式保留，不盲改。
- 官方来源Profile仍采用DiT FP8候选，不能标为原精度无损。
- Heretic不改变官方生成主体，不保证解除所有限制或画质不变。
- Hybrid第一轮使用官方编码器，不同样添加Heretic，不重复在线量化；默认需显式启用。
- 没有用户主机IP、没有安装到用户机器、没有提供“已验证四Spark”的虚假状态。
