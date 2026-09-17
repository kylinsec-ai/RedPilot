<script setup lang="ts">
/**
 * run 状态徽标。旧版在 Runs.svelte 与 Run.svelte 各抄了一份 STATUS_CLS,
 * 这里收成一处:配色走 styles/theme.css 的 .st-* ,文案走 types 的 RUN_STATUS_LABEL。
 */
import { computed } from "vue";
import { RUN_STATUS_LABEL, RUN_STATUSES } from "../types";

const KNOWN = new Set<string>(RUN_STATUSES);

const props = defineProps<{ status: string }>();

const cls = computed(() => "st-tag st-" + (KNOWN.has(props.status) ? props.status : "other"));
const label = computed(() => RUN_STATUS_LABEL[props.status] ?? props.status);
</script>

<template>
  <el-tag :class="cls" size="small" effect="plain" disable-transitions>{{ label }}</el-tag>
</template>
