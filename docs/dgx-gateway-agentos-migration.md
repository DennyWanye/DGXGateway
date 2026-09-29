# DGX Gateway → AgentOS 迁移手册

第一版仍由 Mac `mac-dgx-gateway` 持有 mutation authority。这里交付的是可验证制品与迁移合同，不会修改 AgentOS、网络、防火墙或模型服务。

## 不变量

- `/Users/operator/.codex/locks/dgx-spark-two-node` 仍是唯一 operation lock。
- authority record 是写权限 fence，不是第二把模型锁。真正不可复制的 single-writer oracle 是 head DGX 上 mode-0600 的远端单调 authority CAS；Mac 与 AgentOS 每次 mutation 都必须通过 SSH 核对同一个 epoch/authority/credential hash。本地 authority root 即使被复制也不能产生第二 writer。
- Mac 与 AgentOS 绝不能同时通过 admission。
- 复制 SQLite、配置或 lock 目录不会授予写权限；缺少 matching authority ID、credential、ACTIVE epoch 任一项都 fail-closed。
- AgentOS Nix module 默认 `mutationEnabled = false`，只允许状态比对。

## 正向迁移

1. 停止接收新操作，等待当前 operation terminal；验证唯一锁 `UNLOCKED`。
2. `DGXGateway/manage.sh export ...`，再 `verify`，记录 archive SHA-256。
3. 使用 `python3 -m DGXGateway.dgx_gateway.admission activate-agentos ...` 执行交接。命令先在 SQLite 同一写事务内封闭新 mutation 并证明没有 active operation，再获取并持续 guard 唯一部署锁。随后验证真实 release completion 与 export archive，把 head DGX 的远端 CAS 从 Mac ACTIVE 原子推进为更高 epoch 的 DISABLED；这是所有旧 Mac/副本共同的 fencing point。
4. 启动 AgentOS 只读实例；挂载 config/state/SSH/repo/authority，但不挂载生产 credential，不开放 mutation。
5. 对比两节点、模型、profile、端口、锁与账本摘要完全一致。
6. 远端 CAS 由 head 上 sibling `flock` 线性化完整 read/compare/replace/fsync-dir 事务。同一命令随后把真实 disable/release/export 制品逐个重算 SHA 和 chain，再以 CAS 把 head DGX 远端 fence 推进为 AgentOS ACTIVE，最后写本地 AgentOS record并释放唯一锁；只向 AgentOS 注入 matching credential。旧 Mac每次 mutation 都读取该远端 fence，因此即使它保留完整 authority 目录副本，也立即因 epoch/authority/credential 不匹配而失败。
7. handoff operation 持久记录 lease generation/token capability path/phase/outcome/release receipt。第一道远端 fence 后任一失败均为 `RECOVERY_REQUIRED`；使用相同 `activate-agentos` 命令会根据 local/remote epoch 幂等续跑。只有 remote/local AgentOS epoch、authority ID 和 credential hash reconciliation 一致，才记录 `COMMITTED/RELEASED`。
8. 生成 mode-0600 `handoff.json`，schema 为 `dgx-gateway-authority-handoff-v1`，精确绑定 cluster、from/to authority、Mac disable record SHA、前序 release completion SHA、export archive SHA、目标 epoch，并按 canonical JSON 计算 chain SHA。
9. NixOS 设置 `handoffReceipt` 后才允许将 `mutationEnabled` 设为 true；Gateway admission 会再次机械校验全部 digest、epoch、target authority 和 chain SHA。任一字段缺失/篡改都拒绝写入。
10. 重新运行 admission check 后才允许 AgentOS mutation；Mac 保持 disabled。

## 回退

AgentOS 写门尚未开启时可直接停止只读实例并恢复 Mac。写门开启后，回退必须是一次新的反向 handoff：先停 AgentOS mutation、证明锁 UNLOCKED、写更高 DISABLED epoch，再以更高 ACTIVE epoch 恢复 Mac；不得回放旧 epoch。

## OCI 合同

镜像基于固定的 arm64 Python 3.13 slim digest，非 root、read-only rootfs、drop capabilities。运行时外置挂载 typed config、state、SSH、repo、authority 和 secret。生产 credential 不进入 image、export 或 Git。NixOS 声明位于 `DGXGateway/deploy/agentos/dgx-gateway.nix`，容器内配置固定为 `/run/dgx-gateway/gateway.toml`。
