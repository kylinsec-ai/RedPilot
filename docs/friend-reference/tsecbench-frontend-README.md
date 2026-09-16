# TSecBench 前端控制台

Vue 3 + Vite 实现的 TSecBench 平台前端，用于：

- 连接 TSecBench 平台（`BENCHMARK_TOKEN` 认证）列出题目、查看进度
- 点击题目进入解题页：启动容器、获取提示、手动提交 flag
- 接入 OpenAI 兼容 LLM（DeepSeek / GLM 等），一键让 AI 逐个自动解题并提交 flag
- 「全部自动解」批量顺序处理所有未完成题目

## 运行

```bash
npm install
npm run dev       # http://localhost:5173
```

生产构建：

```bash
npm run build     # 产物在 dist/
npm run preview
```

## 配置（浏览器设置页，自动保存到 localStorage）

| 配置 | 说明 |
|------|------|
| BENCHMARK_BASE_URL | 平台地址，如 `http://127.0.0.1:8000` |
| BENCHMARK_TOKEN | 跑分任务 Token（UUID），创建任务后下发 |
| LLM Base URL | OpenAI 兼容接口，如 `https://api.deepseek.com/v1` |
| LLM API Key / 模型 | LLM 凭证 |
| 自动获取提示 / 最大轮数 / 自动关容器 | AI 解题行为选项 |

## 注意

1. **CORS**：后端 `api.py` 未配置跨域中间件。开发模式已通过 Vite 代理解决
   （`vite.config.js` 把 `/openapi` 转发到 `http://127.0.0.1:8000`），
   设置页的 baseUrl 填 `http://localhost:5173` 即可同源访问。
   生产部署需用 Nginx 反向代理把 `/openapi` 转发到后端。
2. **VPN**：题目容器地址需连接靶场 VPN 后才有意义；AI 只能基于题目描述/提示
   推断 flag，无法真正访问靶场目标，适合推理类题目。
3. LLM 输出解析：优先取 JSON 数组，其次 `flag{...}` 正则，再退化为 `flag=xxx` 行。