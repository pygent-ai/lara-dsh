"""仓内挂载（vendored）skills 及其到用户 skills 目录的自动 provision。

本包不是运行时子系统，而是随包分发的 skill 数据的宿主：
``dynamic-memory-cli/`` 内含 SKILL.md 与单文件 CLI 脚本，首次使用永续
会话时由 :func:`lara.skills.provision.ensure_dynamic_memory_skill` 复制
到 ``<user lara root>/skills/dynamic-memory-cli/``。
"""
