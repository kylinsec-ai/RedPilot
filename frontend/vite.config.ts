import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  server: {
    // dev 时后端 = 本地观测平台(宿主裸跑 :8090;worker 容器 :8080 仅本地调试)
    proxy: { "/api": "http://127.0.0.1:8090" },
  },
  build: {
    // 产物即交付物：web/ 被 Docker COPY + bind-mount，须提交 git
    outDir: "../web",
    emptyOutDir: true,
  },
});
