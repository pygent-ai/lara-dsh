"""Lara 接入与运维层（人机入口）。

承载 CLI 等面向人和运维流程的适配器；不属于 agent 核心功能。依赖方向：
``lara_shell.ops`` 可以 import agent 内核与运行托管，agent 内核绝不 import
``lara_shell``。未来拆仓时本包随 ``lara_shell`` 整体迁往环境层仓。
"""
