# NavelMaker 2 Desktop 使用说明

NavelMaker 2 Desktop 是一个本地小说模拟准备与运行工具。它会把小说文本抽取成多层 JSON 数据库，用于长期角色模拟、世界演化、Agent 决策、用户自由干预和偏离原著后的事件推演。

## 快速开始

按顺序运行这三个批处理文件：

```bat
01_install_requirements.bat
02_prepare_simulation.bat
03_run_simulation.bat
```

`01_install_requirements.bat` 会安装 Python 依赖。

`02_prepare_simulation.bat` 会打开准备界面。选择小说 TXT 文件，设置读取比例，填写或检查本地 LLM 配置，然后开始准备数据。

`03_run_simulation.bat` 会检查必需数据库是否存在，并打开独立模拟界面。

## 本地 LLM 要求

本程序需要一个 OpenAI-compatible 的本地 LLM API，例如 LM Studio、Ollama 兼容服务或其他兼容 `/v1/chat/completions` 的服务。

默认配置保存在：

```text
settings.json
```

默认字段：

```json
{
  "llm_base_url": "http://localhost:1234/v1",
  "llm_model": "gemma-4-26b-a4b-it",
  "llm_api_key": "lm-studio"
}
```

准备界面会保存 LLM 配置，模拟界面会使用同一套配置。

## 准备流程

在准备界面中：

1. 选择小说 TXT。
2. 设置 Story percentage。
3. 检查 Base URL、Model、API key。
4. 点击 Check server 确认本地 LLM 可用。
5. 点击开始准备。

准备流程会执行 Step 1-16，核心产物会发布到 `generated_db` 文件夹。

当前 Step 9 的处理上限是前 30 个 chunk。若小说样本不足 30 个 chunk，则会处理当前范围内的全部 chunk。

## 输出目录结构

正式数据输出在：

```text
generated_db/
```

世界相关数据库：

```text
generated_db/world/
  novel_ontology.json
  raw_graph_triples.json
  mention_weak_relations.json
  normalized_graph_triples.json
  canonical_relationships_db.json
  relationship_arc_db.json
  structured_world_graph.json
  world_db.json
  canonical_novel_db.json
  simulation_state_db.json
  runtime_event_db.json
  simulation_state.json
```

角色相关数据库：

```text
generated_db/characters/
  mention_alias_index.json
  canonical_entities.json
  character_state_db.json
```

Agent 相关数据库：

```text
generated_db/agents/
  agent_profiles.json
```

根目录中可能保留兼容副本，但模拟界面优先使用 `generated_db` 中的正式数据。

## 三层世界状态系统

Step 15 是整个模拟系统的根基。它不再只生成一个“小说总结数据库”，而是生成三层世界状态系统。

### Canonical Novel DB

文件：

```text
generated_db/world/canonical_novel_db.json
```

用途：

- 保存原著完整轨道。
- 保存角色成长线、关系发展线、事件链、物品流转、能力解锁路径、组织变化和世界规则。
- 作为大型原著存档和基线。
- 不直接决定模拟当前状态。

### Simulation State DB

文件：

```text
generated_db/world/simulation_state_db.json
```

用途：

- 用户从某个时间点开始模拟时，从 Canonical Novel DB 截断生成当前世界状态。
- 只保存当前已经发生、已经拥有、已经知道、已经建立的内容。
- 能力、物品、身份、关系不会因为原著未来结局而提前发放。

### Runtime Event DB

文件：

```text
generated_db/world/runtime_event_db.json
```

用途：

- 保存未来可能发生、正在等待触发、已完成或被阻断的事件队列。
- 原著事件是默认压力和参考轨道，不是强制脚本。
- 用户偏离原著后，事件可以继续、改变、延迟或被阻断。

## 能力、物品、身份与获得系统

资源系统由 Canonical Novel DB、Dependency Graph 和 Acquisition System 共同维护。

核心原则：

- 不把能力、物品、身份直接绑定到角色最终结果。
- 区分原著拥有者和模拟中当前拥有者。
- 区分专属型资源和开放型资源。
- 所有获得、失去、使用、升级、转移都必须通过条件判断和事件触发。

专属型资源示例：

- 血脉限定。
- 武魂限定。
- 身份限定。
- 只有某角色或某类角色可获得。

开放型或机缘型资源示例：

- 谁到达某地点。
- 谁触发某事件。
- 谁接触某物品。
- 谁满足组织、关系、知识或环境条件。

相关数据位于：

```text
generated_db/world/canonical_novel_db.json
generated_db/world/world_db.json
```

其中包含：

- `resources`
- `dependency_graph`
- `acquisition_system`
- acquisition conditions
- loss conditions
- use conditions
- upgrade conditions
- transfer conditions

## 关系系统

关系抽取分为两个阶段。

### Mention-level Weak Relations

文件：

```text
generated_db/world/mention_weak_relations.json
```

这是 Entity Resolution 之前的弱连接证据，不是最终人物关系。

它会抽取：

- 同场景共现。
- 称呼。
- 动作关联。
- 事件共同参与。
- 地点共现。
- 物品共用。
- 明确别名。
- 称号。
- 变身或形态变化。

这些弱连接只作为 resolver 的证据，帮助判断 mention 是否可能属于同一实体或相关实体。

### Canonical Relationships

文件：

```text
generated_db/world/canonical_relationships_db.json
```

Entity Resolution 完成后，弱连接会 normalize 到 canonical entity，形成 canonical relationships。

### Relationship Arc DB

文件：

```text
generated_db/world/relationship_arc_db.json
```

该文件保存人物与人物之间的关系弧，用于 Agent 社交记忆和运行时关系变化追踪。

模拟中关系不会按最终原著结局锁死。关系变化必须通过 runtime event 提交，例如：

- 共同经历。
- 冲突。
- 救助。
- 承诺。
- 背叛。
- 组织关系变化。
- 用户干预。

## 模拟运行

运行：

```bat
03_run_simulation.bat
```

模拟界面会读取：

```text
generated_db/world/world_db.json
generated_db/world/canonical_novel_db.json
generated_db/world/simulation_state_db.json
generated_db/world/runtime_event_db.json
generated_db/world/canonical_relationships_db.json
generated_db/world/relationship_arc_db.json
generated_db/characters/character_state_db.json
generated_db/agents/agent_profiles.json
```

运行时状态保存在：

```text
generated_db/world/simulation_state.json
```

如果 world DB 指纹变化，旧 `simulation_state.json` 会自动备份并重建，以避免读取旧世界状态造成冲突。

## 模拟中的 Agent

Agent 会根据以下内容做决策：

- 当前 Simulation State。
- 当前 Runtime Event Queue。
- 当前场景。
- 角色可见记忆。
- relationship arc 当前状态。
- 已经获得的能力、物品、身份。
- 当前知识范围。
- 世界规则与可用证据。

Agent 不应该根据原著最终结局直接行动。

## 用户偏离原著

用户可以从任意角色的原著锚点开始模拟，并自由偏离原著。

例如：

- 不去触发原著事件。
- 提前离开地点。
- 把某件物品交给其他角色。
- 让非原著拥有者尝试获得开放型资源。
- 改变角色关系。
- 阻断、延迟或改写某个事件。

系统会保留原著基线，但运行时以当前世界状态为准。

## 测试与验证

语法检查：

```bat
python -m py_compile relationship_state_layers.py db_output_layout.py world_state_layers.py step17_runtime.py pipeline_program.py app_files.py simulation_ui.py prepare_ui.py
```

检查模拟所需文件是否存在：

```bat
python -c "from app_files import SIMULATION_REQUIRED_FILES, file_status; print([i['name'] for i in file_status(SIMULATION_REQUIRED_FILES) if not i['exists']])"
```

如果输出为空列表，说明运行所需文件齐全。

## 主要 Python 文件

```text
prepare_ui.py
```

准备界面。

```text
pipeline_program.py
```

Step 1-16 主准备管线。

```text
world_state_layers.py
```

三层世界状态、依赖图和获得系统构建。

```text
relationship_state_layers.py
```

弱关系、canonical relationships 和 relationship arc 构建。

```text
db_output_layout.py
```

将数据库发布到 `generated_db/world`、`generated_db/characters`、`generated_db/agents`。

```text
step17_runtime.py
```

模拟运行时、事件提交、状态分支、World Validator、GM、世界推演和沉浸式场景生成。

```text
simulation_ui.py
```

独立模拟界面。

## 注意事项

- Step 15 是模拟根基，修改时要保持三层 DB、资源系统、关系系统和 runtime contract 一致。
- Entity Resolution 不依赖最终人物关系，而依赖 mention-level weak relations。
- 弱关系不是最终关系，最终关系必须在 canonical_entities 生成后再构建。
- 能力、物品、身份、关系都应由事件和条件驱动，不应按章节自动发放。
- `generated_db/world/test_*.json` 或 `smoke_*.json` 是测试状态文件，不是正式运行必需文件。
