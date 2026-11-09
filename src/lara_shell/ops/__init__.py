"""Lara 接入与运维层（人机入口）。

承载 CLI 等面向人和运维流程的适配器；不属于 agent 核心功能。依赖方向：
``ops`` 可以 import agent 核心与运行托管，agent 核心绝不 import ``ops``。
未来拆仓时本包整体迁往独立的 ops 仓。
"""
