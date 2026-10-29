# Story2World Frontend

React + Vite + TypeScript 前端，只通过 Story2World API 访问小说项目，不直接访问 JSON、Pipeline 或模型服务。

## 本地开发

复制环境变量示例：

```powershell
Copy-Item .env.example .env.local
```

默认内容：

```env
VITE_API_BASE=http://localhost:8000
```

安装并启动：

```bash
npm install
npm run dev
```

Windows 也可以依次双击：

```text
01_install_frontend.bat
02_run_frontend.bat
```

打开 `http://localhost:5173`。

## 生产构建

创建 `.env.production`：

```env
VITE_API_BASE=http://api.shadowrui.com
```

构建：

```bash
npm run build
```

或双击：

```text
03_build_frontend.bat
```

生产文件输出到 `dist/`。

## 上传到 VPS

```bash
scp -r dist/* root@SERVER_IP:/var/www/story2world/
```

Nginx 示例：

```nginx
server {
    listen 80;
    server_name example.com www.example.com;

    root /var/www/story2world;
    index index.html;

    location / {
        try_files $uri /index.html;
    }
}
```

最终用户访问：

```text
https://example.com
```

前端自动请求 `.env.production` 中配置的 API，例如：

```text
http://api.shadowrui.com
```

如果前端网站使用 HTTPS，API 也必须提供 HTTPS，例如
`https://api.shadowrui.com`，否则浏览器会阻止 HTTP 混合内容请求。

## API 与安全

- 所有请求统一经过 `src/api/client.ts`。
- API 地址只读取 `VITE_API_BASE`。
- 网络连接失败只显示友好的世界服务连接提示。
- 页面不会展示服务器文件路径、模型服务地址或后端 traceback。
- 用户名、项目映射和 session_id 保存在当前浏览器 localStorage。
- 创建项目页可调用 `POST /projects/source-preview`，在处理前预览所选 chunk 附近剧情。
- 聊天页可调用 `GET /projects/:projectId/chat/anchor-preview`，按选中角色预览 DB anchor、身份、能力、地点、关系和资料缺口。

## 模拟界面

- 对话生成期间显示角色理解、时间 Agent、附近角色、局部世界、GM 裁定和场景渲染阶段。
- 角色侧栏显示血量、姿态、心情、当前活动、目标、装备、持有物和能力。
- Chat API 返回后使用真实运行时角色状态更新侧栏。
- Chat API 返回 `story_progress`、`rag_orchestration_summary` 和 `recovery_snapshot`。保存后重新进入同一 session，会在对话顶部显示上次存档回顾。
- 角色列表包含 dynamic reference agent；即使不是预制 agent profile，只要后端角色 DB 有记录也可以作为开局角色。

## 页面

- `/`：首页与打开已有项目
- `/projects/new`：上传、估算和创建项目
- `/projects/:projectId/run`：启动与查看处理进度
- `/projects/:projectId`：Dashboard、角色、关系与世界资料
- `/projects/:projectId/chat`：选择角色并开始模拟
