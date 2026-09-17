<script setup lang="ts">
/**
 * 应用根:只有 locale 提供者与外壳。
 * 语言包走 Element 官方的 zh-cn(分页器/表格空态/日期等内置文案)。
 */
import zhCn from "element-plus/es/locale/lang/zh-cn";
import AppShell from "./layout/AppShell.vue";
</script>

<template>
  <el-config-provider :locale="zhCn">
    <AppShell />
  </el-config-provider>
</template>
