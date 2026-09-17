<script setup lang="ts">
/**
 * run 详情:概览指标 + 解出 flag + 折叠时间线 + 原始事件分页日志。
 *
 * 轮询语义(与旧版一致,改动前先读):
 *  · 只依赖 runId —— run 切换时由 router 的 :key 重建本组件。
 *    若把 detail/entries 也算进依赖,每次刷新都会 cancel 计时器立刻再刷,退化成忙轮询。
 *  · running 每 4s 续一次;首次刷新失败(detail 仍为 null)时 2s 重试(瞬时失败不杀轮询);
 *    任一终态即停。
 *  · 切后台跳过 fetch 时不碰 fatal:旧报错留到下次成功刷新再清。
 *  · 时间线 entries 只追加、按 seq 作 key。
 */
import { onBeforeUnmount, onMounted, ref } from "vue";
import { fetchRun, fetchRunEvents, fetchRunTimeline } from "../api/obs";
import { fmtDateTime } from "../lib/format";
import { challengeHref } from "../lib/route";
import type { RunEventRow, RunRow, TimelineEntry, TimelineMeta } from "../types";
import Banner from "../components/Banner.vue";
import Chip from "../components/Chip.vue";
import FlagRow from "../components/FlagRow.vue";
import StatusTag from "../components/StatusTag.vue";
import Timeline from "../components/detail/Timeline.vue";

const props = defineProps<{ runId: string }>();

const detail = ref<RunRow | null>(null);
const fatal = ref("");
const entries = ref<TimelineEntry[]>([]);
const meta = ref<TimelineMeta | null>(null);
const nextSeq = ref(0);
// 原始事件分页日志
const evRows = ref<RunEventRow[]>([]);
const evAfter = ref(0);
const evEnd = ref(false);
const evLoading = ref(false);
const evLoaded = ref(false);

async function loadTimeline(): Promise<void> {
  const tl = await fetchRunTimeline(props.runId, nextSeq.value);
  meta.value = tl.meta;
  for (const en of tl.entries) entries.value.push(en);
  nextSeq.value = tl.next_seq;
}

async function refresh(): Promise<void> {
  const d = await fetchRun(detail.value ? detail.value.run_id : props.runId);
  detail.value = d;
  await loadTimeline();
  if (!evLoaded.value) void loadMoreEvents(); // 首帧即拉第一页原始事件
}

let stopped = false;
let timer: ReturnType<typeof setTimeout> | null = null;

async function tick(): Promise<void> {
  try {
    if (!document.hidden) {
      await refresh();
      fatal.value = "";
    }
  } catch (e) {
    if (!detail.value) fatal.value = String(e);
  }
  if (stopped) return;
  const s = detail.value?.status;
  if (s === "running") timer = setTimeout(() => void tick(), 4000);
  else if (s === undefined) timer = setTimeout(() => void tick(), 2000);
}

onMounted(() => void tick());
onBeforeUnmount(() => {
  stopped = true;
  if (timer) clearTimeout(timer);
});

async function loadMoreEvents(): Promise<void> {
  if (evLoading.value || evEnd.value) return;
  evLoading.value = true;
  try {
    const r = await fetchRunEvents(props.runId, evAfter.value, 500);
    for (const e of r.events) evRows.value.push(e);
    evAfter.value = r.next_seq;
    evEnd.value = r.end;
    evLoaded.value = true;
  } catch (e) {
    fatal.value = String(e);
  } finally {
    evLoading.value = false;
  }
}
</script>

<template>
  <div v-if="fatal && !detail">
    <a class="backlink" href="#/runs">← 返回 Runs 历史</a>
    <Banner tone="err" :text="`加载失败：${fatal}`" />
  </div>
  <div v-else-if="!detail">
    <a class="backlink" href="#/runs">← 返回 Runs 历史</a>
    <div class="loading">加载 run {{ runId }}…</div>
  </div>
  <template v-else>
    <a class="backlink" href="#/runs">← 返回 Runs 历史</a>

    <div class="panel">
      <div class="headline">
        <h1 class="mono" style="font-size: 12.5px; font-weight: 700">
          run {{ detail.run_id.slice(0, 12) }}…
        </h1>
        <StatusTag :status="detail.status" />
        <Chip v-if="detail.status === 'running'" tone="live" text="● 求解中 · 数据随推送增长" />
      </div>
      <div class="mono dim" style="font-size: 11.5px" :title="detail.run_id">
        {{ detail.run_id }}
      </div>

      <div class="rungrid">
        <div>
          <div class="mlabel">题目</div>
          <div class="mval">
            <a class="mono rowlink" :href="challengeHref(detail.challenge_code)"
              >{{ detail.challenge_code }} ↗</a
            >
          </div>
        </div>
        <div>
          <div class="mlabel">worker</div>
          <div class="mval mono">{{ detail.worker_id }}</div>
        </div>
        <div>
          <div class="mlabel">模型</div>
          <div class="mval mono" style="overflow-wrap: break-word">{{ detail.model || "—" }}</div>
        </div>
        <div>
          <div class="mlabel">开始</div>
          <div class="mval tnum">{{ fmtDateTime(detail.started_at) }}</div>
        </div>
        <div>
          <div class="mlabel">结束</div>
          <div class="mval tnum">
            {{ detail.ended_at ? fmtDateTime(detail.ended_at) : detail.status === "running" ? "…" : "—" }}
          </div>
        </div>
        <div>
          <div class="mlabel">时长</div>
          <div class="mval tnum">
            {{ detail.duration_s != null ? detail.duration_s.toFixed(1) + " s" : "—" }}
          </div>
        </div>
        <div>
          <div class="mlabel">轮次 / 会话</div>
          <div class="mval tnum">{{ detail.turns ?? "—" }} / {{ detail.sessions ?? "—" }}</div>
        </div>
        <div>
          <div class="mlabel">发现 flag / 事件行</div>
          <div class="mval tnum">{{ detail.flags_found ?? 0 }} / {{ detail.event_count }}</div>
        </div>
      </div>
    </div>

    <Banner
      v-if="detail.error"
      :tone="detail.status === 'failed' ? 'err' : 'warn'"
      :text="`错误：${detail.error}`"
    />
    <Banner
      v-if="detail.status === 'interrupted'"
      tone="warn"
      text="此 run 被中断（心跳超时 / worker 换题 / 重启残留）——时间线可能不全。"
    />

    <div class="panel">
      <h2>
        解出 flag
        <span v-if="(detail.flags_accepted || []).length" class="sub">
          · {{ (detail.flags_accepted || []).length }} 个
        </span>
      </h2>
      <template v-if="(detail.flags_accepted || []).length">
        <FlagRow v-for="f in detail.flags_accepted" :key="f" :flag="f" />
      </template>
      <div v-else class="mut" style="font-size: 13px">
        {{
          detail.status === "solved"
            ? "平台侧已判解出，但 FLAG 明文未回传（旧会话或异常路径）。"
            : "此 run 未解出任何 flag。"
        }}
      </div>
    </div>

    <div class="panel">
      <h2>
        时间线
        <span v-if="meta && meta.sessions != null" class="sub">
          · {{ meta.sessions }} 个会话 · {{ entries.length }} 条摘要{{
            detail.status === "running" ? " · 推送追加中" : ""
          }}
        </span>
      </h2>
      <Timeline v-if="entries.length" :entries="entries" />
      <div v-else class="loading">还没有事件记录——事件由 worker 推送落库后这里出现。</div>
    </div>

    <div class="panel">
      <h2>
        原始事件
        <span class="sub">· {{ evRows.length }}{{ evEnd ? "" : "+" }} 行</span>
      </h2>
      <el-collapse class="evcoll">
        <el-collapse-item v-for="e in evRows" :key="e.seq" :name="e.seq">
          <template #title>
            <span class="tnum dim" style="margin-right: 10px">{{ e.seq }}</span>
            <span class="evtype mono">{{ e.type }}</span>
          </template>
          <pre class="evpayload mono">{{ e.payload }}</pre>
        </el-collapse-item>
      </el-collapse>
      <el-button
        v-if="!evLoaded"
        size="small"
        style="margin-top: 8px"
        @click="void loadMoreEvents()"
      >
        加载事件…
      </el-button>
      <el-button
        v-else-if="!evEnd"
        size="small"
        style="margin-top: 8px"
        :disabled="evLoading"
        @click="void loadMoreEvents()"
      >
        {{ evLoading ? "加载中…" : "加载更多" }}
      </el-button>
    </div>
  </template>
</template>

<style scoped>
.headline {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 2px;
}
.rungrid {
  display: grid;
  gap: 10px 24px;
  margin-top: 12px;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
}
.rowlink {
  font-weight: 600;
  color: var(--blue);
}
.rowlink:hover {
  text-decoration: underline;
}
.evtype {
  border: 1px solid var(--line2);
  border-radius: 4px;
  padding: 0 6px;
  font-size: 10.5px;
  color: var(--cyan);
}
.evpayload {
  margin: 0 0 8px 4px;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  border-radius: 6px;
  background: color-mix(in srgb, var(--panel2) 60%, transparent);
  padding: 8px 10px;
  line-height: 1.6;
  color: var(--mut);
}
</style>
