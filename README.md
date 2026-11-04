# 人格工坊 / Persona Workshop

一个把 persona-continuum 的人格制作与 Room 运行时嵌入 AstrBot 的插件。

> 本仓库是插件市场发行版：仅保留普通情感、关系与陪伴模拟。

插件不是简单地把几行提示词写进人格设定：它把 continuum 的结构化人格包、八维研究链路、动态 Room 上下文、关系/情绪状态、记忆后端、人格工具和可选静默会诊接入 AstrBot 的聊天流程。

## 功能

- 在 AstrBot WebUI 左侧“扩展功能”中提供人格工坊页面
- 使用 AstrBot 已配置的模型或本地 CLI 执行器制作人格
- 保留 continuum 的制作链路：资料/文件、研究策略、八维 ResearchArtifact、审阅、编译
- 人格保存后写入 AstrBot 原生人格，并可作为默认人格使用
- 兼容 continuum 不同版本/不同模型产出的字段别名：
  - 时间线、台词锚点、情绪触发、决策习惯、失败模式、身体信息等不会再因字段名不同而丢失
  - 静态人格提示词与 Room 运行时共用同一套归一化结果
- 使用 continuum 人格时，每轮消息走隐藏 Room/Session：
  - 动态注入身份内核、情绪、需求、目标、关系立场和行为约束
  - 追加长期人格档案（身份、时间线、关系、表达习惯、台词锚点），避免 Room 丢掉静态设定
  - 回复后提交 affect / relationship / memory 状态
- 自动检测 LivingMemory：
  - 有 LivingMemory 时，长期记忆由 LivingMemory 提供，避免重复注入
  - 没有 LivingMemory 时，自动回退 continuum 自带记忆
- 注册三个人格工具：
  - `persona_search_memories`
  - `persona_get_relationship`
  - `persona_get_runtime_state`
- 可选静默多专家会诊：
  - 主持人 + 心理/关系专家 + 对话策略专家 + 连续性审阅专家
  - 综合结果只作为内部 brief 注入，不发送给用户
  - 默认关闭，避免额外模型调用

## 安装

1. 将本目录放到 AstrBot 的插件目录，例如：

   ```text
   AstrBot/data/plugins/astrbot_plugin_persona_workshop
   ```

2. 安装 `requirements.txt` 中的 Python 依赖。
3. 重启 AstrBot，或在 WebUI 插件页重载插件。
4. 刷新 AstrBot WebUI，进入“扩展功能 → 人格工坊”。

插件自带 `vendor/persona_continuum` 运行代码，因此不需要单独启动 persona-continuum 服务。

## 使用

### 制作人格

1. 打开“人格工坊 → 完整制作”
2. 填写角色名、来源、世界观/备注和本地资料
3. 选择执行器：
   - AstrBot 模型：使用 AstrBot 已配置的 provider
   - 本地 CLI：例如 Gemini CLI (agy)、Codex、Grok Build 等
4. 选择研究方式和公开研究策略
5. 开始创建
6. 在任务中心查看进度；可暂停、恢复、取消、重试或继续研究
7. 完成后在人格库点“使用”，会写入 AstrBot 原生人格并设为默认人格

### Room 运行时

对来自人格工坊的 AstrBot 人格，插件会为每个 AstrBot 会话维护一个隐藏 Room。聊天时会动态组装人格上下文，回复后提交状态，不需要打开 Room UI。

如果 Room 运行时失败，插件会保留静态人格提示词作为回退，不会中断聊天。

## 配置

配置文件由 AstrBot 插件配置系统管理。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `native_prefix` | `人格工坊·` | 写入 AstrBot 原生人格时的名称前缀 |
| `max_source_chars` | `30000` | 创建时读取资料的最大字符数 |
| `room_runtime_enabled` | `true` | 是否启用 Room 动态人格 |
| `memory_mode` | `auto` | `auto` / `continuum` / `livingmemory` / `hybrid` |
| `livingmemory_top_k` | `5` | Room 从 LivingMemory 读取的记忆条数 |
| `livingmemory_inject` | `false` | 是否由本插件注入 LivingMemory 结果；LivingMemory 自己的召回 hook 已注入时建议关闭 |
| `consultation_mode` | `off` | `off` / `complex` / `always` |
| `consultation_min_chars` | `120` | `complex` 模式触发会诊的最小消息长度 |
| `consultation_provider_id` | 空 | 会诊使用的 Provider；留空使用当前模型 |

## 联网研究

公开人物联网研究需要真实搜索来源，不会用模型训练记忆冒充联网：

- 本地 CLI 执行器可能自带 web search/fetch 工具
- AstrBot 侧可配置 Tavily / Bocha 等搜索源
- 两者都不可用时，联网研究会在预检阶段明确失败
- 本地资料、引导访谈和虚构/私人人物模式不受影响

## 架构

```text
AstrBot 消息
    ↓
人格工坊 Runtime Adapter
    ├─ 识别 continuum 原生人格
    ├─ 选择记忆后端（LivingMemory / continuum / hybrid）
    ├─ 隐藏 Room：prepare_turn
    ├─ 可选静默会诊
    ├─ 注入动态人格上下文
    └─ on_agent_done：commit_turn
           ├─ 更新情绪/需求
           ├─ 更新关系
           └─ 写入记忆
```

主要文件：

- `main.py`：插件入口、Web API、人格工具、AstrBot hooks
- `schema_compat.py`：continuum 产物字段归一化（静态提示词与 Room 共用）
- `room_runtime.py`：隐藏 Room 会话、动态上下文、状态提交
- `livingmemory_bridge.py`：LivingMemory 自动检测与检索
- `consultation.py`：静默会诊
- `continuum_runtime.py`：内置 continuum 服务图
- `vendor/persona_continuum`：vendored continuum 运行代码

## 隐私

仓库中不包含：

- 用户聊天记录
- QQ 号或其他账号信息
- API Key、Token、密码
- AstrBot 运行时数据库或插件数据目录
- 本地绝对路径

插件运行时数据保存在 AstrBot 的：

```text
data/plugin_data/astrbot_plugin_persona_workshop/
```

模型 API Key 仍由 AstrBot 配置管理，不在本仓库中。

## 第三方代码与许可证

`vendor/persona_continuum` 来自 persona-continuum 项目，按其 MIT License 分发。许可证文件见：

```text
vendor/persona_continuum/LICENSE
```

本插件的集成代码目前没有单独声明许可证文件。使用或再分发前请自行确认插件代码的授权方式。

## 已知限制

- 已按 AstrBot 4.29.4 的环境开发和验证
- 公开联网研究的速度取决于搜索源或本地 CLI 的能力
- 静默会诊默认关闭，开启后会增加模型调用次数和延迟
- 人格工具只在人格工坊 Room 回合中返回有效结果
