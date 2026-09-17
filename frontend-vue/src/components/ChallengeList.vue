<script setup lang="ts">
/**
 * 侧栏题目列表:搜索 + 按难度分组。
 *
 * reveal 逻辑(深链/数据到达/过滤变化时滚到激活行)是这里最讲究的一段:
 * lastScrolled 是副作用记录而非渲染输入,故用普通变量 —— 否则 10s 轮询每次换
 * 快照都会重跑并拽滚。另外两条边界是踩过的坑:
 *   · 搜索吞掉激活行 → 清空搜索让下一轮再滚;
 *   · 全量花名册都没有该题 → **绝不**碰 query,否则每次击键都被本 effect 抹掉。
 */
import { computed, ref, watch } from "vue";
import ChallengeRowView from "./ChallengeRow.vue";
import {
  DIFF_ORDER,
  diffGroupLabel,
  diffKey,
  rosterData,
  rosterFailed,
  sortRows,
  toRows,
  type DiffKey,
} from "../stores/roster";
import type { ChallengeRow } from "../types";

// 激活题号由外壳传入(路由输入显式化);花名册仍读共享单例
const props = withDefaults(defineProps<{ activeCode?: string | null }>(), { activeCode: null });

const reduceMq = window.matchMedia("(prefers-reduced-motion: reduce)");

// 搜索题号子串;不编码进 hash——列表状态由常驻挂载自然保留
const query = ref("");
const listEl = ref<HTMLDivElement | null>(null);

const q = computed(() => query.value.trim().toLowerCase());
const all = computed(() => toRows(rosterData.value));
const nTotal = computed(() => all.value.length);
// q 过滤后的行(列表与 reveal 逻辑共用一份,避免两处 filter 语义漂移)
const filtered = computed(() =>
  q.value ? all.value.filter((r) => r.unique_code.toLowerCase().includes(q.value)) : all.value,
);

// 先全局默认排序再按难度装桶(桶内相对顺序与逐桶排序一致,sort stable);
// 未知难度归"其他",组序 DIFF_ORDER
const groups = computed(() => {
  const sorted = sortRows(filtered.value);
  const buckets = new Map<DiffKey, ChallengeRow[]>();
  for (const r of sorted) {
    const k = diffKey(r.difficulty);
    const arr = buckets.get(k) ?? [];
    arr.push(r);
    buckets.set(k, arr);
  }
  const out: { key: DiffKey; label: string; rows: ChallengeRow[] }[] = [];
  for (const k of DIFF_ORDER) {
    const rows = buckets.get(k);
    if (rows?.length) out.push({ key: k, label: diffGroupLabel(k), rows });
  }
  return out;
});

let lastScrolled = "";
watch(
  () => [props.activeCode, q.value, nTotal.value, rosterData.value] as const,
  () => {
    const c = props.activeCode;
    if (!c) {
      lastScrolled = ""; // 回到总览即清,重访同题仍 reveal
      return;
    }
    if (c === lastScrolled || document.hidden) return;
    const present = all.value.some((r) => r.unique_code === c);
    if (present && !filtered.value.some((r) => r.unique_code === c)) {
      // 搜索吞掉了激活行 → 清空搜索,下一轮再滚
      query.value = "";
      return;
    }
    if (!present) return; // 见文件头注释:此处绝不能碰 query
    const el = listEl.value?.querySelector<HTMLElement>(`[data-code="${c}"]`);
    if (!el) return; // 行尚未渲染(数据未到)→ 不记 lastScrolled,数据到达后重试
    el.scrollIntoView({ block: "nearest", behavior: reduceMq.matches ? "auto" : "smooth" });
    lastScrolled = c;
  },
);

function onSearchKeydown(e: KeyboardEvent): void {
  // 仅清搜索,不冒泡——外壳的抽屉 Esc 关抽屉,一次按键只做一件事
  if (e.key === "Escape") {
    e.stopPropagation();
    query.value = "";
  }
}
</script>

<template>
  <div class="search">
    <input
      v-model="query"
      type="search"
      placeholder="搜索题号…"
      aria-label="搜索题目"
      data-autofocus
      class="search-input"
      @keydown="onSearchKeydown"
    />
  </div>

  <div v-if="rosterFailed && !rosterData" class="hint">花名册暂不可用，自动重试中…</div>
  <div v-else-if="!rosterData" class="hint">加载中…</div>
  <div v-else-if="nTotal === 0" class="hint">还没有题目数据</div>
  <div v-else-if="groups.length === 0" class="hint">无匹配题目</div>
  <div v-else ref="listEl" class="groups">
    <div v-for="grp in groups" :key="grp.key">
      <div class="grp-head">
        <span class="grp-label">{{ grp.label }}</span>
        <span class="grp-count tnum">{{ grp.rows.length }}</span>
      </div>
      <div class="grp-rows">
        <ChallengeRowView
          v-for="r in grp.rows"
          :key="r.unique_code"
          :row="r"
          :active="r.unique_code === props.activeCode"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.search {
  padding: 0 8px 6px;
}
.search-input {
  width: 100%;
  border: 1px solid var(--line2);
  border-radius: 6px;
  background: var(--bg);
  padding: 4px 10px;
  font-size: 12.5px;
  color: var(--ink);
  outline: none;
}
.search-input::placeholder {
  color: var(--dim);
}
.search-input:focus {
  border-color: var(--blue);
}
.hint {
  padding: 8px 14px;
  font-size: 12px;
  color: var(--dim);
}
.groups > * + * {
  margin-top: 12px;
}
.grp-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 0 10px 4px;
}
.grp-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--dim);
}
.grp-count {
  font-size: 10.5px;
  color: var(--dim);
  opacity: 0.7;
}
.grp-rows > * + * {
  margin-top: 2px;
}
</style>
