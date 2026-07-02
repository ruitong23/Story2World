# NavelMaker 2 Web API

这个目录是现有 `navelmaker2_desktop` 外面的一层 FastAPI。它不修改 Step 1–16，而是在每个用户、每个项目自己的目录中调用原 pipeline。

## 启动

1. 双击 `01_install_requirements.bat`
2. 确保 LM Studio 等 OpenAI-compatible 本地接口已经运行
3. 双击 `02_run_api.bat`
4. 浏览器打开 `http://localhost:8000/docs`

如果需要修改模型配置，可在启动 BAT 前设置：

```bat
set NOVEL_LLM_BASE_URL=http://localhost:1234/v1
set NOVEL_LLM_MODEL=gemma-4-26b-a4b-it
set NOVEL_LLM_API_KEY=lm-studio
set NAVELMAKER_ALLOWED_ORIGINS=https://你的前端域名.example
```

## 用户数据

用户名不需要密码，只用作本机文件分区，不是安全认证。目录格式：

```text
users/
  用户名/
    user.json
    projects/
      project_xxx/
        raw.txt
        project.json
        status.json
        logs.jsonl
        db/
          graph/
          canonical/
          agents/
          runtime/
        sessions/
          session_001/
            runtime/
              simulation_state.json
              runtime_event_db.json
              runtime_relationship_db.json
              runtime_log.json
            agents/
              runtime_agent_state.json
              runtime_agent_dbs_index.json
              runtime_agent_dbs/
```

同一个用户名再次进入时，可调用 `GET /users/{username}/projects` 找回项目。

## 推荐前端流程

1. `POST /users` 创建或恢复用户名
2. `POST /projects/estimate` 上传 TXT 做 chunk 数量与耗时估算
3. 用户选择 chunk 数量
4. 可选调用 `POST /projects/source-preview`，在真正创建项目前扫一眼所选 chunk 附近约 3000 字的剧情
5. `POST /projects`，字段包含 `username`、`file`、`selected_chunks`
6. 轮询 `GET /projects/{project_id}/status?username=...`
7. ready 后读取 dashboard、characters、relationships、world
8. 可选调用 `GET /projects/{project_id}/chat/anchor-preview`，按角色生成 DB anchor 开局预览
9. 通过 `POST /projects/{project_id}/chat` 或 `/chat/stream` 运行模拟

默认 chunk size 是 3000，overlap 是 300；估算按每个 chunk 60 秒，再加约 45 秒的启动和下游数据库整理开销。实际运行状态会返回 `current_chunk`、`processing_chunk_total`、`current_batch`、`processing_batch_total`、`elapsed_seconds` 和 `estimated_remaining_seconds`。

## 当前适配

- API 同时兼容旧 `generated_db/` 和新 `db/graph`、`db/canonical`、`db/runtime`、`db/agents` 布局。
- `relationships` 优先读取 `canonical_relationship_db.json`，并过滤 `CO_OCCURS_IN_SCENE` 等弱共现关系和自环。
- `characters` 不再只允许预制 agent；只要角色存在 `character_id`，Step17 可用 dynamic reference agent 开局。
- Chat 返回会附带 `story_progress`、`rag_orchestration_summary` 和 `recovery_snapshot`。
- `POST /projects/{project_id}/chat/save` 会生成恢复摘要；再次打开同一个 session 时，`GET /chat/session` 会返回该摘要。
- 每个 session 独立写 `sessions/{session_id}/runtime` 和 `sessions/{session_id}/agents/runtime_agent_dbs/`，避免不同用户/角色互相污染。

## 部署提醒

如果网站只有你自己在运行后端的这台电脑上打开，前端可请求 `http://localhost:8000`。如果其他互联网用户也要使用你这台电脑的算力，他们浏览器里的 `localhost` 指向的是他们自己的电脑；你还需要为本 API 配置 HTTPS 反向代理、端口转发或安全隧道，并把 `NAVELMAKER_ALLOWED_ORIGINS` 设置为你的真实网站域名。当前用户名没有密码，不能当作公网安全认证。
