import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  server: {
    // dev 时后端 = 本地服务端(宿主裸跑 :8000;worker 容器 :8080 仅本地调试)
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  build: {
    // 产物即交付物：web/ 被 Docker COPY + bind-mount，须提交 git
    outDir: "../web",
    emptyOutDir: true,
  },
});
