<script setup lang="ts">
/**
 * 工具调用行:行内展开完整输出。
 * 展开态存活的机制:父级 entries 只追加且按 seq 作 key ⇒ 轮询追加不会重建本组件。
 */
import { ref } from "vue";
import type { ToolEntry } from "../../types";

defineProps<{ entry: ToolEntry }>();

const open = ref(false);

function toggle(): void {
  open.value = !open.value;
}
function onKeydown(e: KeyboardEvent): void {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    toggle();
  }
}
</script>

<template>
  <div class="tool" :class="entry.err ? 'tool-err' : 'tool-ok'">
    <div
      role="button"
      tabindex="0"
      :aria-expanded="open"
      class="row"
      @click="toggle"
      @keydown="onKeydown"
    >
      <span class="marker mono dim">▶</span>
      <span class="cmd mono">{{ entry.tool || "tool" }} {{ entry.cmd || "" }}</span>
      <span class="status mono" :class="entry.err ? 'c-red' : 'c-green'">
        {{ entry.err ? "✖ 出错" : "✔ 完成"
        }}{{ entry.out_len != null ? ` · ${entry.out_len} 字符` : "" }}
      </span>
    </div>
    <pre v-if="open && entry.out" class="out mono">{{ entry.out }}</pre>
  </div>
</template>

<style scoped>
.tool {
  margin: 7px 0;
  border: 1px solid var(--line);
  border-left-width: 3px;
  border-radius: 7px;
  background: var(--panel2);
}
.tool-ok {
  border-left-color: var(--green);
}
.tool-err {
  border-left-color: var(--red);
}
.row {
  display: flex;
  cursor: pointer;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 10px;
}
.marker {
  margin-top: 1px;
  font-size: 12px;
}
.cmd {
  min-width: 0;
  flex: 1;
  font-size: 12.5px;
  overflow-wrap: break-word;
  word-break: break-all;
}
.status {
  margin-top: 2px;
  white-space: nowrap;
  font-size: 11px;
}
.out {
  margin: 0;
  max-height: 320px;
  overflow-y: auto;
  white-space: pre-wrap;
  overflow-wrap: break-word;
  border-top: 1px dashed var(--line);
  padding: 8px 12px 10px;
  font-size: 12px;
  color: #c9d3e5;
}
</style>
