# DGX Cluster Gateway

统一管理 DGX 模型生命周期、资源互斥、持久任务和 MCP 调用。当前实现支持两机与四机适配，创作工作台是可选业务层。

请从仓库根目录的 [安装指南](../docs/INSTALL.zh-CN.md) 开始，再读 [模型目录](../docs/models/README.zh-CN.md) 和 [安全说明](../SECURITY.md)。

`config/gateway.example.toml` 是脱敏后的保守示例。必须替换路径、节点与凭据文件位置；默认 `mutation_enabled=false`，不会授予模型切换权限。`manage.sh` 面向 macOS launchd，Linux 部署须另建本机服务单元。

源码、示例配置和本地运行状态是三种不同资源。不要提交正在使用的 `gateway.toml`、state 目录或导出备份。MCP 提交后沿原 job/operation ID 查询；超时不能当作未受理，不能换新幂等键重试生成。
