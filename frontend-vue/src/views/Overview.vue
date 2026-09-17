<script setup lang="ts">
/**
 * 题目总览。数据来自共享花名册 store(App 启动时已挂 10s 单轮询),
 * 挂载时补刷一次 —— 恢复旧版"回总览即刷新"的手感。
 *
 * 依赖读凭据:用户在凭据门处补录 token 后必须立即重拉,否则会停在挂载时那次
 * 401 的错误态,直到手动刷新或 10s 轮询才恢复(实测过)。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { fmtRel } from "../lib/format";
import { readAuth } from "../stores/auth";
import { challengeHref } from "../lib/route";
import {
  refreshRoster,
  rosterData,
  rosterFailed,
  sortRows,
  toRows,
  traceChips,
  type RowSortKey,
} from "../stores/roster";
import type { ChallengeRow } from "../types";
import Banner from "../components/Banner.vue";
import Chip from "../components/Chip.vue";
import DiffBadge from "../components/DiffBadge.vue";
import StatCard from "../components/StatCard.vue";

const snap = computed(() => rosterData.value);

onMounted(() => void refreshRoster());
watch(
  () => readAuth.value,
  () => void refreshRoster(),
);

const sortKey = ref<RowSortKey | null>(null);
const sortAsc = ref(true);

// el-table 的 sortable="custom" 只给 {prop, order};映射回花名册的排序键
const PROP_TO_KEY: Record<string, RowSortKey> = {
  unique_code: "code",
  total_score: "score",
  correct_flag_count: "prog",
  last_activity: "act",
};
function onSortChange({ prop, order }: { prop: string | null; order: string | null }): void {
  const k = prop ? PROP_TO_KEY[prop] : undefined;
  if (!k || !order) {
    sortKey.value = null;
    sortAsc.value = true;
    return;
  }
  sortKey.value = k;
  sortAsc.value = order === "ascending";
}

const rows = computed(() => toRows(snap.value));
const n = computed(() => rows.value.length);
const comp = computed(() => rows.value.filter((x) => x.is_completed).length);
const flagTotal = computed(() => rows.value.reduce((a, x) => a + (x.correct_flag_count || 0), 0));
const localFlags = computed(() => rows.value.filter((x) => x.local?.flag).length);
const sorted = computed(() => sortRows(rows.value, sortKey.value, sortAsc.value));

/** 每行的本地痕迹徽标,按题号缓存一次 —— 表格里 v-if/v-for 各调一次会算两遍 */
const chipsByCode = computed(() => {
  const m = new Map<string, ReturnType<typeof traceChips>>();
  for (const r of sorted.value) m.set(r.unique_code, traceChips(r.local));
  return m;
});

const EMPTY_TEXT = "还没有题目数据——worker 首次轮询平台后自动出现。";

// 整行可点(旧版是 <tr onclick>):单元格内的链接自行 stop,不会重复导航
const router = useRouter();
function onRowClick(row: ChallengeRow): void {
  router.push({ name: "challenge", params: { code: row.unique_code } });
}
</script>

<template>
  <Banner
    v-if="rosterFailed && !snap"
    tone="err"
    text="无法读取题目总览（/api/roster 不可达）。服务是否在运行？"
  />
  <div v-else-if="!snap" class="loading">加载中…</div>
  <template v-else>
    <Banner
      v-if="rosterFailed"
      tone="warn"
      text="题目总览刷新失败，正在显示缓存数据，自动重试中…"
    />
    <h1 class="page-title">任务总览</h1>

    <div class="grid-metrics" style="margin: 16px 0 14px">
      <StatCard label="题目总数" :value="n" />
      <StatCard label="已完成" :value="comp" tone="green" />
      <StatCard label="待解" :value="n - comp" />
      <StatCard label="已确认 flag" :value="flagTotal" suffix=" 个" />
      <StatCard label="本地留痕（FLAG 文件）" :value="localFlags" tone="amber" />
    </div>

    <Banner
      v-if="snap.platform_disabled"
      tone="info"
      text="平台轮询已禁用（缺 BENCHMARK_* 或 SDK 不可用）——下表仅含本地有痕迹的题。"
    />
    <Banner
      v-else-if="snap.platform_error"
      tone="warn"
      :text="`平台数据暂不可用：${snap.platform_error}。正在显示 ${fmtRel(snap.fetched_at)}的缓存，本地痕迹照常刷新。`"
    />
    <div v-else-if="snap.fetched_at" class="dim" style="font-size: 12px; margin-bottom: 8px">
      平台数据 {{ fmtRel(snap.fetched_at) }}更新 · 共 {{ n }} 题
    </div>

    <div class="panel" style="padding: 0">
      <el-table
        :data="sorted"
        size="small"
        :empty-text="EMPTY_TEXT"
        row-key="unique_code"
        class="clickable-rows"
        @row-click="onRowClick"
        @sort-change="onSortChange"
      >
        <el-table-column prop="unique_code" label="题号" sortable="custom" min-width="150">
          <template #default="{ row }">
            <a :href="challengeHref(row.unique_code)" class="rowlink mono" @click.stop>
              <span :class="{ 'c-green': row.is_completed }">{{ row.unique_code }}</span>
            </a>
            <span v-if="row.local_only" class="only-local">仅本地</span>
          </template>
        </el-table-column>
        <el-table-column label="难度" width="92">
          <template #default="{ row }"><DiffBadge :difficulty="row.difficulty" /></template>
        </el-table-column>
        <el-table-column prop="total_score" label="分值" sortable="custom" width="92" align="right">
          <template #default="{ row }">
            <span class="tnum">{{ row.total_score ?? "—" }}</span>
          </template>
        </el-table-column>
        <el-table-column
          prop="correct_flag_count"
          label="flag 进度"
          sortable="custom"
          width="112"
          align="right"
        >
          <template #default="{ row }">
            <span class="tnum">
              {{ row.correct_flag_count || 0 }}/{{ row.flag_count || 0
              }}{{ row.is_completed ? " ✓" : "" }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="本地痕迹" min-width="190">
          <template #default="{ row }">
            <span v-if="chipsByCode.get(row.unique_code)?.length" class="chips">
              <Chip
                v-for="c in chipsByCode.get(row.unique_code) || []"
                :key="c.text"
                :tone="c.tone"
                :text="c.text"
              />
            </span>
            <span v-else class="dim" style="font-size: 11.5px">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="last_activity" label="最近活动" sortable="custom" width="126">
          <template #default="{ row }">
            <span class="dim" style="font-size: 12px">
              {{ row.local?.last_activity ? fmtRel(row.local.last_activity) : "—" }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="" width="86">
          <template #default="{ row }">
            <a :href="challengeHref(row.unique_code)" class="mut" style="font-size: 12px"
              >查看 →</a
            >
          </template>
        </el-table-column>
      </el-table>
    </div>
  </template>
</template>

<style scoped>
.clickable-rows :deep(.el-table__row) {
  cursor: pointer;
}
.rowlink {
  font-weight: 600;
  font-size: 12.5px;
  color: inherit;
}
.rowlink:hover {
  text-decoration: underline;
}
.only-local {
  margin-left: 4px;
  font-size: 11px;
  color: var(--amber);
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
</style>
