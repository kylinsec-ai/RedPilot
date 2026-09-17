<script setup lang="ts">
/**
 * 侧栏单行题目。信息密度是刻意的:题号色表状态(绿=已完成/亮=当前/灰=未触碰),
 * 右侧 {已确认}/{总数},"进行中"才给一条 2px 进度条。
 * 本地痕迹徽标最多 2 个护密度(traceChips 已按意义排序:FLAG✓ 最先),余数以 +N 明示。
 */
import { computed } from "vue";
import Chip from "./Chip.vue";
import { traceChips } from "../stores/roster";
import { challengeHref } from "../lib/route";
import type { ChallengeRow } from "../types";

const props = withDefaults(defineProps<{ row: ChallengeRow; active?: boolean }>(), {
  active: false,
});

const done = computed(() => !!props.row.is_completed);
const flags = computed(() => props.row.flag_count || 0);
const got = computed(() => props.row.correct_flag_count || 0);
// 进度条只出现在"进行中"(未完成且有确认数);完成/未触碰行用题号色 + ✓ 区分,不加条
const inProg = computed(() => !done.value && got.value > 0);
const pct = computed(() => (flags.value > 0 ? Math.round((got.value / flags.value) * 100) : 0));
const allChips = computed(() => traceChips(props.row.local));
const chips = computed(() => allChips.value.slice(0, 2));
const extra = computed(() => allChips.value.length - chips.value.length);

const codeCls = computed(() =>
  done.value ? "c-green" : props.active ? "c-ink" : "mut",
);
</script>

<template>
  <a
    :href="challengeHref(row.unique_code)"
    :data-code="row.unique_code"
    :aria-current="active ? 'page' : undefined"
    class="chrow"
    :class="{ 'chrow-active': active }"
  >
    <span class="line1">
      <span class="code mono" :class="codeCls">{{ row.unique_code }}</span>
      <span v-if="row.local_only" class="only-local">仅本地</span>
      <span class="prog tnum" :class="{ 'c-green': done }">
        {{ got }}/{{ flags }}{{ done ? " ✓" : "" }}
      </span>
    </span>
    <span v-if="inProg || chips.length" class="line2">
      <span v-if="inProg" class="bar"><span class="bar-fill" :style="{ width: pct + '%' }"></span></span>
      <span v-if="chips.length" class="chips">
        <Chip v-for="c in chips" :key="c.text" :tone="c.tone" :text="c.text" />
        <span v-if="extra" class="extra tnum" title="更多本地痕迹">+{{ extra }}</span>
      </span>
    </span>
  </a>
</template>

<style scoped>
.chrow {
  display: block;
  border-radius: 7px;
  padding: 5px 10px;
  line-height: 1.35;
  color: inherit;
}
.chrow:hover {
  background: color-mix(in srgb, var(--panel2) 50%, transparent);
  text-decoration: none;
}
.chrow-active {
  background: var(--panel2);
}
.line1 {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.code {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
}
.only-local {
  flex: none;
  font-size: 10.5px;
  color: var(--amber);
}
.prog {
  margin-left: auto;
  flex: none;
  font-size: 11px;
  color: var(--dim);
}
.line2 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}
.bar {
  display: block;
  height: 2px;
  min-width: 0;
  flex: 1;
  overflow: hidden;
  border-radius: 999px;
  background: var(--line);
}
.bar-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: color-mix(in srgb, var(--amber) 80%, transparent);
}
.chips {
  display: flex;
  flex: none;
  gap: 4px;
}
.extra {
  font-size: 10.5px;
  color: var(--dim);
}
</style>
