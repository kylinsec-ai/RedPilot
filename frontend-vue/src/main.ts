import { createApp } from "vue";
// Element Plus 暗色变量:本产品只有暗色(index.html 的 <html class="dark"> 常驻),
// 不做明暗切换。组件本身由 unplugin-vue-components 按需引入,这里不需要全量注册。
// 状态不用 pinia:各 store 是模块级单例 ref(见 stores/),共享同一份即可。
import "element-plus/theme-chalk/dark/css-vars.css";
import "./styles/theme.css";
import App from "./App.vue";
import router from "./router";

createApp(App).use(router).mount("#app");
