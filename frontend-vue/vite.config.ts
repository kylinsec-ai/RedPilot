import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import AutoImport from "unplugin-auto-import/vite";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需:组件走 Components 解析器,ElMessage/ElMessageBox 这类
    // 函数式 API 走 AutoImport(否则收不到类型)。
    AutoImport({ resolvers: [ElementPlusResolver()] }),
    Components({ resolvers: [ElementPlusResolver()] }),
  ],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    // dev 时后端 = 本地服务端(宿主裸跑 :8000;worker 容器 :8080 仅本地调试)
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  build: {
    // 产物即交付物:web/ 被 Docker COPY + bind-mount,须提交 git
    outDir: "../web",
    emptyOutDir: true,
  },
});
