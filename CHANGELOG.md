# Changelog

## 0.3.0 (2026-11-05)

- 新增人格工坊页面与完整制作链路：资料研究、八维提取、审阅、编译。
- 新增 continuum 字段归一化，兼容不同版本/模型产出的时间线、台词锚点、情绪触发、决策习惯等字段。
- 新增隐藏 Room 运行时，每轮动态注入身份、情绪、需求、目标和关系状态，回复后提交状态。
- 新增 LivingMemory 自动检测，支持 `auto` / `continuum` / `livingmemory` / `hybrid` 记忆模式。
- 新增人格工具：`persona_search_memories`、`persona_get_relationship`、`persona_get_runtime_state`。
- 新增可选静默多专家会诊，默认关闭。
- 生成的人格写入 AstrBot 原生人格，并支持设为默认人格。
