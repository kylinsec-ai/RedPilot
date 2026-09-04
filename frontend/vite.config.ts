import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  server: {
    // dev 时后端跑在 worker 容器 :8080（status_server），SSE 经 http-proxy 无缓冲直通
    proxy: { "/api": "http://127.0.0.1:8080" },
  },
  build: {
    // 产物即交付物：web/ 被 Docker COPY + bind-mount，须提交 git
    outDir: "../web",
    emptyOutDir: true,
  },
});
