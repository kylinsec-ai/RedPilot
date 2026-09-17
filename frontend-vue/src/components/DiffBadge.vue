<script setup lang="ts">
/** 难度徽标:中文名 + 基调全部来自 format.ts 的 diffLabel(单源,未知原样透出)。 */
import { computed } from "vue";
import { diffLabel } from "../lib/format";

const props = defineProps<{ difficulty: string | undefined }>();

const label = computed(() => diffLabel(props.difficulty));
const type = computed(() => {
  const tone = label.value[1];
  return tone === "easy" ? "success" : tone === "medium" ? "warning" : tone === "hard" ? "danger" : "info";
});
</script>

<template>
  <el-tag :type="type" size="small" round effect="plain" disable-transitions>{{ label[0] }}</el-tag>
</template>
