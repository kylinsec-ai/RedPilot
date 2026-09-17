<script setup lang="ts">
/**
 * 小徽标(本地痕迹 / 时间线状态 / 事件类型共用)。
 * 用 Element 的 tag 做结构,但**配色沿用旧版六基调**——EP 的 success/warning
 * 取值与本产品调色板不同,直接映射会把"绿=已解出""红=崩溃"的语义冲淡。
 * 覆盖走 --el-tag-* 变量(见 styles/theme.css 的 .chip-*),不复制 EP 的样式。
 */
import { computed } from "vue";

const props = defineProps<{
  tone?: "flag" | "crash" | "sess" | "art" | "live" | "plain";
  text: string;
}>();

const cls = computed(() => "chip chip-" + (props.tone ?? "plain"));
</script>

<template>
  <el-tag :class="cls" size="small" effect="plain" disable-transitions>{{ text }}</el-tag>
</template>
