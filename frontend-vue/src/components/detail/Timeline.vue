<script setup lang="ts">
/**
 * 时间线:先把条目散排(flat),遇 session 起新块 —— 头行 + 缩进组,
 * 其后条目归入该组直到下个 session 头。
 * (旧版靠未闭合 div 的浏览器自动恢复,2+ 会话会产生累积缩进;此处按良构分组。)
 *
 * 刻意不用 Element 的 timeline 组件:它是"等距节点 + 描述"的通用形态,
 * 而这里的语义是"会话分块 + 缩进条目 + 可展开工具输出",换成通用组件反而要
 * 拆掉领域信息。保留自研结构,配色走 Element 的 CSS 变量。
 */
import { computed } from "vue";
import { fmtClock } from "../../lib/format";
import type { SessionEntry, TimelineEntry } from "../../types";
import TimelineRow from "./TimelineRow.vue";

const props = defineProps<{ entries: TimelineEntry[] }>();

type Block = { header: SessionEntry | null; items: TimelineEntry[] };

const blocks = computed<Block[]>(() => {
  const out: Block[] = [];
  for (const e of props.entries) {
    if (e.kind === "session") {
      out.push({ header: e, items: [] });
    } else if (!out.length) {
      out.push({ header: null, items: [e] });
    } else {
      out[out.length - 1].items.push(e);
    }
  }
  return out;
});

const blockKey = (b: Block) => (b.header ? `s${b.header.seq}` : "flat");

// attempt 行时间戳:相隔 15s 内只显示一次(旧版 lastShownTs 规则,按序全流扫描)
const attemptTs = computed(() => {
  const map = new Map<number, string>();
  let lastShown = 0;
  for (const e of props.entries) {
    const show = !e.t || e.t - lastShown > 15000;
    if (e.t) lastShown = e.t;
    if (e.kind === "attempt" && show) map.set(e.seq, fmtClock(e.t));
  }
  return map;
});
</script>

<template>
  <template v-for="b in blocks" :key="blockKey(b)">
    <template v-if="b.header">
      <div class="sess-head">
        <span class="sess-title">会话开始</span>
        <span class="mono dim" style="font-size: 11.5px">
          {{ fmtClock(b.header.t) }}{{ b.header.sid ? ` · id ${b.header.sid}` : "" }}
        </span>
      </div>
      <div class="sess-body">
        <div v-if="b.header.note" class="mono mut" style="font-size: 12.5px">
          {{ b.header.note }}
        </div>
        <TimelineRow v-for="e in b.items" :key="e.seq" :entry="e" :ts="attemptTs.get(e.seq)" />
      </div>
    </template>
    <template v-else>
      <TimelineRow v-for="e in b.items" :key="e.seq" :entry="e" :ts="attemptTs.get(e.seq)" />
    </template>
  </template>
</template>

<style scoped>
.sess-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-top: 18px;
}
.sess-title {
  font-size: 14px;
  font-weight: 700;
}
.sess-body {
  margin-left: 9px;
  margin-top: 2px;
  border-left: 1px solid var(--line2);
  padding-bottom: 4px;
  padding-left: 16px;
}
.sess-body > * + * {
  margin-top: 6px;
}
</style>
