<script setup lang="ts">
/**
 * 原始 transcript 尾部(调试用)。**没打开就不发请求。**
 * 打开时随父轮询(tick 变化)重新拉取;每轮先回到"加载中…",取回后替换。
 * stale 标志防止已关闭/已卸载的实例被迟到的响应写回。
 *
 * open 是双向绑定(v-model:open):父级 Challenge 用它抑制时间线的自动滚动
 * —— 用户正在读原始 transcript 时不该被新条目拽走。
 */
import { computed, ref, watch } from "vue";
import { fetchTranscript } from "../../api/obs";

const props = defineProps<{ code: string; tick: number }>();
const open = defineModel<boolean>("open", { default: false });

/** el-collapse 的 v-model 是数组形态,这里当受控组件用 */
const activeNames = computed(() => (open.value ? ["raw"] : []));
function onCollapseChange(names: string | number | (string | number)[]): void {
  open.value = Array.isArray(names) ? names.includes("raw") : names === "raw";
}

const text = ref("加载中…");

watch(
  () => [open.value, props.tick, props.code] as const,
  ([isOpen], _old, onCleanup) => {
    if (!isOpen) return;
    let stale = false;
    text.value = "加载中…";
    fetchTranscript(props.code, 200)
      .then((jd) => {
        if (stale) return;
        text.value = (jd.lines || []).join("\n") || "(空)";
      })
      .catch((err) => {
        if (stale) return;
        text.value = "加载失败：" + String(err);
      });
    onCleanup(() => {
      stale = true;
    });
  },
  { immediate: true },
);
</script>

<template>
  <el-collapse
    class="rawcoll"
    :model-value="activeNames"
    @update:model-value="onCollapseChange"
  >
    <el-collapse-item name="raw">
      <template #title>
        <span style="font-size: 12.5px; color: var(--dim)">
          原始 transcript 尾部（JSONL，调试用）
        </span>
      </template>
      <pre class="raw mono">{{ text }}</pre>
    </el-collapse-item>
  </el-collapse>
</template>

<style scoped>
.rawcoll {
  --el-collapse-border-color: var(--line);
  --el-collapse-header-bg-color: transparent;
  --el-collapse-content-bg-color: transparent;
  margin-top: 6px;
}
.raw {
  margin: 0;
  max-height: 340px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: #0a0f1b;
  padding: 10px;
  font-size: 11.5px;
  line-height: 1.5;
  color: #9fb0cd;
}
</style>
