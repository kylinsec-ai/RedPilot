<script setup lang="ts">
/**
 * 单条 flag:明文 + 一键复制(1.2s 后回落)。剪贴板失败静默 —— 明文就在眼前,
 * 复制失败不该弹一个打断阅读的错误。
 */
import { onBeforeUnmount, ref } from "vue";

const props = defineProps<{ flag: string }>();

const copied = ref(false);
let timer: ReturnType<typeof setTimeout> | null = null;

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.flag);
    if (timer) clearTimeout(timer);
    copied.value = true;
    timer = setTimeout(() => (copied.value = false), 1200);
  } catch {
    /* 剪贴板不可用:明文仍可直接选中复制 */
  }
}

onBeforeUnmount(() => {
  if (timer) clearTimeout(timer);
});
</script>

<template>
  <div class="flagrow">
    <span class="flag mono">{{ flag }}</span>
    <el-button size="small" text @click="copy">{{ copied ? "已复制" : "复制" }}</el-button>
  </div>
</template>

<style scoped>
.flagrow {
  display: flex;
  max-width: 100%;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel2);
  padding: 6px 10px;
  margin-bottom: 4px;
}
.flag {
  flex: 1;
  min-width: 0;
  word-break: break-all;
  font-size: 13px;
  color: var(--green);
}
</style>
