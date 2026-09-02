# docs/screenshots — Workspace 演示截图

README 的 Workspace 章节引用这里的 4 张截图（对应 handoff 文档 §27）：

| 文件名 | 画面要求 |
|---|---|
| `workspace-overview.png` | 一屏同框：Conversation + Current Run + Active Task + Eval Pulse |
| `agent-trace.png` | 多 Agent 轨迹：Supervisor → Memory/Reflect/Plan → Chat，含工具调用 |
| `memory-delta.png` | 知识演化：Memory Delta 内容 + source run（Drawer → Memory tab） |
| `evaluation-loop.png` | 评测闭环：Candidate + Failure Type + Evolution Loop（Drawer → Evaluation tab） |

截图方法：启动后端后打开 `http://localhost:8000/workspace/`（或 `npm run dev` 的 5173 端口），浏览器 1440×900 下截取全屏。
