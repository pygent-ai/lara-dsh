# 待办事项(Backlog)

仓库级事项清单。新事项追加到末尾,完成后移入"已完成"并注明提交。

---

## B-1 代码美化工程:对齐 pygent 原生 API,清理手挖字典与重复校验

**状态**:待开始 · **优先级**:中 · **来源**:2026-11-22 context-budget 接线修复(见 `verification-2026-11-21.md` 第四轮与同日 sidecar 改动)

### 背景与样板案例

修复 pet 上下文预算接线时,第一版实现写了这样的代码:

```python
declared = mapping["models"][model]["capabilities"]["limits"]["context_tokens"]
if isinstance(declared, int) and declared > 0:
    config.context_window = (
        min(inherited, declared)
        if isinstance(inherited, int) and inherited > 0
        else declared
    )
```

问题:pygent 的 `ModelConfig.from_mapping()` 在上一行刚把字典解析成类型化对象,
却回头去挖原始字典;`isinstance` 校验重复了 pygent 自己保证的不变量
(`ModelLimits.__post_init__` 保证 `context_tokens` 为 None 或正整数,
`RunConfig.__post_init__` 保证 `context_window` 同样)。定稿版本用类型化属性链
加 `min()` 一行收敛(见 `src/lara_pet/sidecar.py::_apply_lara_model`)。

### 判定标准(什么算"丑")

1. **手挖已解析对象背后的原始字典**:依赖库已提供 dataclass/属性链(如
   `ModelConfig.models[key].spec.capabilities.limits`),代码却对源字典做多层
   键访问。判断依据:同一函数(或前几行)里存在对应的 `from_mapping`/解析调用。
2. **重复校验库已保证的不变量**:对库方 dataclass `__post_init__` 已约束的
   字段再做 `isinstance`/正数检查。库源码里能找到对应校验的,一律算。
3. **手拼字典再喂给 `from_mapping`**:pygent 提供
   `ModelConfig.from_components(connections=..., models=..., model_groups=...)`
   等类型化构造器,项目却手工拼嵌套 dict 再解析。
4. **同一事实双源不接线**:同一进程内两个数据源本应一致(如声明的
   `limits.context_tokens` 与 `RunConfig.context_window`),代码各用各的。
   此类不只是丑,是缺陷;发现即单独修复,不等美化工程。

### 已知候选点(审计起点,非完整清单,逐个判断后再动手)

- `src/lara_shell/ops/cli/credentials.py:108` —
  `config.model_config_mapping["models"][entry.key]` 挖原始映射;同一 config
  上已有类型化的 `config.model_config`(`ModelConfig`),判断标准 1 的典型。
- `src/lara_shell/dsh_settings.py::build_model_mapping` — 手拼完整嵌套 dict
  再 `ModelConfig.from_mapping`;pygent 有 `from_components` 类型化构造器,
  判断标准 3 的典型。注意两个调用方(sidecar 借路与 pet 自有模型)共享此函数,
  改造需同时过两边测试。
- `src/lara_pet/sidecar.py::_borrow_dsh_model`(206-245 行)— 对 dsh 网关的
  wire dict(`route.get("default")`、`route["account"]`、`route["apiKey"]`)
  手工解包。wire 层用 dict 不一定是丑;若 dsh gateway 能返回类型化条目再改,
  否则记录"受 dsh 接口限制"后保留。
- `src/lara_shell/config/editor.py:60` — 配置编辑器直接操作原始 dict。若它
  编辑的是"待写回 YAML 的文件表示",dict 是合法形态;审计时先确认语义再定性。

### 审计方法

1. 用固定 grep 模式扫全仓:`\["models"\]\[|\["capabilities"\]|\["limits"\]`、
   `from_mapping\(` 的调用方清单、对已解析 config 对象的 `.get(` 链。
2. 每个命中点先查 pygent(以 `.venv` 锁定版本为准,勿读其它环境的
   site-packages——两处副本结构有差异,如 `ModelEntry.spec` 包装)是否已提供
   类型化路径;有则改,无则记录保留原因。
3. 每处修改前先补测试锁定现状行为,再重构;行为必须不变。

### 完成标准

- 上述 grep 模式零命中(或有明确注释说明保留原因:wire 边界/文件表示)。
- 所有改动点测试先行,`pytest` 与 `pyright` 不劣化(存量 1 个
  `PetSpeakChannel.deliver` 签名告警除外)。
- 本清单中每个候选点都有结论:改了(附提交)或保留(附原因)。

### 明确不做

- 不改行为、不升级依赖、不重排与判定标准无关的代码风格问题(命名/格式交给
  linter)。
