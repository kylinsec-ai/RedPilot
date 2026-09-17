<script setup lang="ts">
/**
 * 状态横幅。信息层级(信息/警告/错误)沿用旧版三档,改用 Element 的 alert
 * 承担结构(图标 + 关闭位),文案由调用方逐字给出。
 */
import { computed } from "vue";

const props = defineProps<{ tone: "info" | "warn" | "err"; text: string }>();

const type = computed(() =>
  props.tone === "err" ? "error" : props.tone === "warn" ? "warning" : "info",
);
</script>

<template>
  <el-alert :type="type" :title="text" :closable="false" show-icon />
</template>
