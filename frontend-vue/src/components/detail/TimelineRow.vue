<script setup lang="ts">
/** 时间线单行:纯按 kind 分派(结构照旧,配色走 token)。 */
import { stopZh } from "../../lib/format";
import type { TimelineEntry } from "../../types";
import Banner from "../Banner.vue";
import ToolRow from "./ToolRow.vue";

defineProps<{ entry: TimelineEntry; ts?: string }>();
</script>

<template>
  <div v-if="entry.kind === 'turn'" class="turn">
    <span style="font-weight: 700">第 {{ entry.n }} 轮</span>
    <span class="mono dim" style="font-size: 11.5px">
      {{ stopZh(entry.stop) }}{{ entry.tokens ? ` · ${entry.tokens} tokens` : "" }}
    </span>
  </div>
  <div v-else-if="entry.kind === 'attempt'" class="attempt">
    <span v-if="ts" class="mono dim" style="font-size: 11.5px; margin-right: 6px">{{ ts }}</span>
    ↻ 重试第 {{ entry.n ?? 0 }} 次（stall/超时后自动重启会话）
  </div>
  <ToolRow v-else-if="entry.kind === 'tool'" :entry="entry" />
  <div v-else-if="entry.kind === 'text'" class="text">
    {{ entry.text || "" }}{{ entry.more ? "…" : "" }}
  </div>
  <div v-else-if="entry.kind === 'note'" class="note">{{ entry.note || "" }}</div>
  <Banner v-else-if="entry.kind === 'error'" tone="err" :text="entry.note || '错误'" />
  <!-- session 不会出现在条目流里(块头单独渲染),此处静默 -->
</template>

<style scoped>
.turn {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 13px 0 2px;
}
.attempt {
  margin: 8px 0 0;
  font-size: 12.5px;
  color: var(--amber);
}
.text {
  margin: 8px 0;
  border-left: 2px solid rgba(90, 162, 255, 0.5);
  border-radius: 0 6px 6px 0;
  background: rgba(90, 162, 255, 0.06);
  padding: 8px 12px;
  color: var(--ink);
  white-space: pre-wrap;
  overflow-wrap: break-word;
}
.note {
  margin-top: 6px;
  font-size: 12.5px;
  color: var(--mut);
}
</style>
