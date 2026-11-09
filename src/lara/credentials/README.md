# Credentials

Reads and writes user credential sources. Command-line parsing belongs in
`lara.ops.cli.credentials`; this package owns storage only.

## 归属判断：随 agent 核心仓走，不进 ops 仓

凭据包是 **agent 构造链的输入库**，不是运维适配层：

- `lara.config.load_run_config` 通过 `load_credentials` 把用户凭据源合并进运行配置；
- `lara.runtime.model_configuration` 通过 `lookup_credential` 为模型层解析密钥；
- CLI（`lara.ops.cli.credentials`）只是这些能力的命令行人机入口，若拆仓后留在
  ops 仓，会把 agent 运行时的必需输入留在运维层，迫使核心反向依赖 ops。

因此未来拆仓时本包随 **lara 主体仓（agent-core）** 迁移；ops 仓仅保留
`lara.ops.cli.credentials`。
