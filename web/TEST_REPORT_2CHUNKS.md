# 《斗罗大陆》2-chunk 本地 LLM 测试报告

测试日期：2026-06-22

## 测试输入

- 文件：`C:\Users\ruitongs\Downloads\《斗罗大陆》_qinkan.net.txt`
- 文件大小：5,871,036 bytes（低于 6 MB 限制）
- 本地模型：`gemma-4-26b-a4b-it`
- Chunk size：3000
- Overlap：300
- 选择数量：2
- 测试用户：`codex_test_2chunks`
- 项目：`project_d91bf622a79b4026`
- 最终保留会话：`test_session_006`

## 时间与进度

- API 估算全文：1154 chunks
- Pipeline 实际建立：1154 chunks
- 选择 2 chunks 的旧估算：120 秒
- 实际完成：162 秒
- 根据实测，API 已改为每 chunk 60 秒加约 45 秒固定开销；2 chunks 新估算为 165 秒。
- Step 9 chunk 进度和 Step 11 batch 进度已经拆分为独立字段。

## 生成结果

- Step 9：2/2 chunks 完成
- 原始节点：29
- 原始边：21
- Canonical entities：16
- Structured graph entities：17
- Structured graph relations：13
- Characters：4
- 可运行 Agents：2
- World entities：12
- Knowledge units：12
- Timeline events：15
- Step 16 preflight：通过

主要角色与事实符合小说开头范围：

- 唐三（full agent）
- 唐昊（light agent）
- 唐大先生
- 唐蓝太爷
- 唐三 `CHILD_OF` 唐昊
- 唐昊 `PARENT_OF` 唐三
- 地点包含圣魂村、鬼见愁
- 能力包含玄天功、玄玉手、紫极魔瞳

## API 检查

以下接口均返回 HTTP 200：

- `/health`
- `/projects/{project_id}/status`
- `/projects/{project_id}/dashboard`
- `/projects/{project_id}/characters`
- `/projects/{project_id}/relationships`
- `/projects/{project_id}/world`
- `/projects/{project_id}/chat`
- `/users/{username}/projects`

修复的问题：

- 修复失效 Python 虚拟环境无法被安装 BAT 自动重建。
- 修复 chat LLM callable 不接受 `temperature` / `max_tokens` 导致 500。
- 修复角色 tier 返回 null。
- 修复角色 relationship_count 返回 0。
- 修复 Step 11 batch 被误报为 chunk。
- 修复多个 session 共用 runtime 和 agent sidecar 文件；每个 session 使用独立的 `runtime/` 与 `agents/`。
- 去除 chat 响应中过大的 `raw_result`。
- 对近重复叙述进行保守去重。
- 增加 pipeline 质量警告、关系证据和来源回退。

## 仍需注意

两个测试 chunk 均为 partial：

- valid chunks：0
- partial chunks：2
- validation errors：25

无效字段和证据已被现有校验器过滤，下游数据库仍成功完成且 Step 16 preflight 通过。API 现在会在 status 中返回这些 warning。

沉浸式对话能够正确说出圣魂村和唐昊父子关系，session 也能继续保存；但模型偶尔会加入两个 chunk 中没有明确证据的背景描述。因此当前对话可用于小说世界模拟，但还不是严格的逐句 grounded QA。API 会在没有逐条运行时 evidence refs 时返回 `grounding: uncertain`。
