<script setup lang="ts">
/**
 * Runs 历史。查询条件(状态下拉 + worker/题号过滤)只在点"查询"或 Enter 时生效 ——
 * 不做逐键请求,所以挂载后只在凭据变化时补拉一次。
 *
 * 过期响应丢弃:手动连点时以最后发起者为准(loadSeq 单调计数)。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { fetchRuns } from "../api/obs";
import { fmtDateTime, fmtDur } from "../lib/format";
import { challengeHref, runHref } from "../lib/route";
import { readAuth } from "../stores/auth";
import { RUN_STATUSES, RUN_STATUS_LABEL, type RunRow } from "../types";
import Banner from "../components/Banner.vue";
import StatCard from "../components/StatCard.vue";
import StatusTag from "../components/StatusTag.vue";

const router = useRouter();

const rows = ref<RunRow[] | null>(null);
const failed = ref("");
const qStatus = ref("");
const qWorker = ref("");
const qCode = ref("");
let loadSeq = 0; // 过期响应丢弃(手动连点时最后发起者胜)

function buildQs(): string {
  const p = new URLSearchParams();
  if (qStatus.value) p.set("status", qStatus.value);
  const w = qWorker.value.trim();
  if (w) p.set("worker", w);
  const c = qCode.value.trim();
  if (c) p.set("challenge", c);
  const qs = p.toString();
  return qs ? "?" + qs : "";
}

async function load(): Promise<void> {
  const qs = buildQs();
  const seq = ++loadSeq;
  try {
    const data = (await fetchRuns(qs)).runs;
    if (seq !== loadSeq) return; // 已有更新的查询在飞:丢弃过期响应
    rows.value = data;
    failed.value = "";
  } catch (e) {
    if (seq !== loadSeq) return;
    failed.value = String(e);
  }
}

const hasFilter = computed(
  () => qStatus.value !== "" || qWorker.value.trim() !== "" || qCode.value.trim() !== "",
);

onMounted(() => void load());
// 依赖读凭据:补录 token 后立即重拉(否则停在挂载时的 401 错误态)
watch(() => readAuth.value, () => void load());

const list = computed(() => rows.value ?? []);
const running = computed(() => list.value.filter((x) => x.status === "running").length);
const solved = computed(() => list.value.filter((x) => x.status === "solved").length);
const bad = computed(
  () => list.value.filter((x) => x.status === "failed" || x.status === "interrupted").length,
);
const events = computed(() => list.value.reduce((a, x) => a + x.event_count, 0));

const EMPTY_TEXT = computed(() =>
  hasFilter.value ? "无匹配记录——调整过滤条件后点查询。" : "还没有运行记录——worker 首次解题推送后自动出现。",
);

function onRowClick(row: RunRow): void {
  router.push({ name: "run", params: { runId: row.run_id } });
}
</script>

<template>
  <h1 class="page-title">运行记录</h1>
  <Banner v-if="failed" tone="err" :text="`加载失败：${failed}`" />

  <div class="grid-metrics" style="margin: 16px 0 14px">
    <StatCard label="记录数" :value="rows?.length ?? '…'" />
    <StatCard label="进行中" :value="running" tone="amber" />
    <StatCard label="已解出" :value="solved" tone="green" />
    <StatCard label="失败 / 中断" :value="bad" tone="red" />
    <StatCard label="入库事件" :value="events" suffix=" 行" />
  </div>

  <div class="panel" style="padding: 0">
    <form class="filters" @submit.prevent="void load()">
      <el-select
        v-model="qStatus"
        aria-label="按状态过滤"
        size="small"
        style="width: 132px"
        placeholder="全部状态"
      >
        <el-option label="全部状态" value="" />
        <el-option
          v-for="s in RUN_STATUSES"
          :key="s"
          :label="RUN_STATUS_LABEL[s] ?? s"
          :value="s"
        />
      </el-select>
      <el-input
        v-model="qWorker"
        size="small"
        placeholder="worker 过滤"
        style="width: 144px"
        class="mono-input"
      />
      <el-input
        v-model="qCode"
        size="small"
        placeholder="题号过滤(如 a-05)"
        style="width: 160px"
        class="mono-input"
      />
      <el-button size="small" native-type="submit" type="primary">查询</el-button>
      <span class="filter-sum dim">
        {{ rows ? `${rows.length} 条 · 平台库事件 ${events} 行` : "加载中…" }}
      </span>
    </form>

    <el-table
      v-if="rows"
      :data="rows"
      size="small"
      class="clickable-rows"
      row-key="run_id"
      :empty-text="EMPTY_TEXT"
      @row-click="onRowClick"
    >
      <el-table-column label="开始时间" width="170">
        <template #default="{ row }">
          <span class="tnum mut" style="font-size: 12px">{{ fmtDateTime(row.started_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="题目" width="112">
        <template #default="{ row }">
          <a class="mono rowlink" :href="challengeHref(row.challenge_code)" @click.stop
            >{{ row.challenge_code }}</a
          >
        </template>
      </el-table-column>
      <el-table-column label="worker" width="106">
        <template #default="{ row }">
          <span class="mono mut" style="font-size: 12px">{{ row.worker_id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="96">
        <template #default="{ row }"><StatusTag :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="时长" width="92">
        <template #default="{ row }">
          <span class="tnum mut">{{ fmtDur(row.duration_s) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="轮次" width="76">
        <template #default="{ row }">
          <span class="tnum mut">{{ row.turns ?? "—" }}</span>
        </template>
      </el-table-column>
      <el-table-column label="flag" width="76">
        <template #default="{ row }">
          <span
            class="tnum c-green"
            :title="(row.flags_accepted || []).join('\n') || undefined"
            >{{ row.flags_found ?? 0 }}{{ (row.flags_accepted || []).length ? " ✓" : "" }}</span
          >
        </template>
      </el-table-column>
      <el-table-column label="事件" width="76">
        <template #default="{ row }">
          <span class="tnum dim">{{ row.event_count }}</span>
        </template>
      </el-table-column>
      <el-table-column label="错误" min-width="180">
        <template #default="{ row }">
          <span class="errcell" :title="row.error || undefined">{{ row.error || "—" }}</span>
        </template>
      </el-table-column>
      <el-table-column label="" width="80">
        <template #default="{ row }">
          <a :href="runHref(row.run_id)" class="mut" style="font-size: 12px" @click.stop
            >查看 →</a
          >
        </template>
      </el-table-column>
    </el-table>
    <div v-else-if="!failed" class="loading">加载中…</div>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--line);
}
.filter-sum {
  margin-left: auto;
  font-size: 12px;
  color: var(--dim);
}
.clickable-rows :deep(.el-table__row) {
  cursor: pointer;
}
.rowlink {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ink);
}
.rowlink:hover {
  text-decoration: underline;
}
.errcell {
  display: inline-block;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
  font-size: 12px;
  color: var(--redlight);
}
</style>
