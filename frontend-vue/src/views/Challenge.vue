<script setup lang="ts">
/**
 * 题目详情:平台行 + 本地痕迹合成,右侧是会话时间线,底部是原始 transcript。
 *
 * 两条容易踩的语义(与旧版一致,改动前先读):
 *  1. entries **只追加**,按 seq 作 key —— 否则轮询追加会重建工具行,展开态全丢。
 *     meta.truncated(worker 侧 digest 被重截断、seq 从头编号)时:先成功取到 after=0
 *     全量再替换,取失败就保留旧条目 —— 否则 keyed 行会与重编的 seq 冲突叠行。
 *  2. 首屏失败才记 fatal;已有数据后的瞬时失败保持现状继续轮询(旧版此处会永久停轮询)。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { fetchChallenge, fetchTimeline } from "../api/obs";
import { fmtClock, fmtDateTime, fmtMB } from "../lib/format";
import { live } from "../stores/live";
import { ACTIVE_PHASES, type ChallengeDetail, type TimelineEntry, type TimelineMeta } from "../types";
import Banner from "../components/Banner.vue";
import Chip from "../components/Chip.vue";
import DiffBadge from "../components/DiffBadge.vue";
import FlagRow from "../components/FlagRow.vue";
import Timeline from "../components/detail/Timeline.vue";
import TranscriptRaw from "../components/detail/TranscriptRaw.vue";

const props = defineProps<{ code: string }>();

const detail = ref<ChallengeDetail | null>(null);
const fatal = ref<string | null>(null);
const meta = ref<TimelineMeta | null>(null);
const entries = ref<TimelineEntry[]>([]);
const nextSeq = ref(0);
const rawOpen = ref(false);
const rawTick = ref(0);
const tlRoot = ref<HTMLDivElement | null>(null);

const liveHere = computed(
  () => ACTIVE_PHASES.includes(live.value.phase) && live.value.challenge_code === props.code,
);

async function refresh(c: string): Promise<void> {
  try {
    const ch = await fetchChallenge(c);
    detail.value = ch;
    let tl = await fetchTimeline(c, nextSeq.value);
    if (tl.meta?.truncated) {
      // worker 侧 digest 被重截断(压缩/5MB 轮换):seq 从头编号,本地旧条目作废
      const fresh = await fetchTimeline(c, 0);
      entries.value.length = 0;
      nextSeq.value = 0;
      tl = fresh;
    }
    meta.value = tl.meta;
    for (const en of tl.entries) entries.value.push(en); // append-only:展开态不丢
    nextSeq.value = tl.next_seq;
    fatal.value = null;
  } catch (e) {
    // 有旧数据时保持现状续轮询;首屏失败记 fatal
    if (!detail.value) fatal.value = String(e);
  }
  rawTick.value += 1; // raw 面板开着时随轮询刷新,关闭时 TranscriptRaw 自行忽略
}

/**
 * 自调度轮询:每轮结束后按当前相位定下次间隔(本页求解中 2s / 其余 4s)。
 * 瞬时失败与后台 tick 不终止循环;切后台跳过 fetch,回前台恢复。
 */
let stopped = false;
let timer: ReturnType<typeof setTimeout> | null = null;

async function tick(): Promise<void> {
  if (stopped) return;
  if (!document.hidden) await refresh(props.code);
  if (stopped) return;
  timer = setTimeout(() => void tick(), liveHere.value ? 2000 : 4000);
}

onMounted(() => void tick());
onBeforeUnmount(() => {
  stopped = true;
  if (timer) clearTimeout(timer);
});

/** 时间线追加重绘后:仅当近底部且未开 raw、页面可见时跟随滚动,绝不拽走正在读旧内容的用户 */
watch(
  () => entries.value.length,
  () => {
    if (document.hidden || !liveHere.value || rawOpen.value) return;
    const el = tlRoot.value;
    if (!el) return;
    const near = el.getBoundingClientRect().bottom - window.innerHeight < 320;
    if (near) el.scrollIntoView({ block: "end" });
  },
);
</script>

<template>
  <div v-if="fatal">
    <a class="backlink" href="#/">← 返回题目总览</a>
    <Banner tone="err" :text="`加载失败：${fatal}`" />
  </div>
  <div v-else-if="!detail">
    <a class="backlink" href="#/">← 返回题目总览</a>
    <div class="loading">加载题目 {{ code }}…</div>
  </div>
  <template v-else>
    <a class="backlink" href="#/">← 返回题目总览</a>

    <div class="panel">
      <div class="headline">
        <h1 class="mono" style="font-size: 12.5px; font-weight: 700">{{ detail.unique_code }}</h1>
        <DiffBadge :difficulty="detail.difficulty" />
        <Chip v-if="liveHere" tone="live" text="● 求解中" />
        <Chip v-if="detail.is_completed" tone="flag" text="已完成" />
        <span v-if="detail.local_only" style="font-size: 11px; color: var(--amber)"
          >仅本地痕迹</span
        >
      </div>

      <el-collapse v-if="detail.description" class="desccoll">
        <el-collapse-item name="desc">
          <template #title>
            <span style="font-size: 13px; color: var(--blue)">题目描述</span>
          </template>
          <div class="desc-body mut">{{ detail.description }}</div>
        </el-collapse-item>
      </el-collapse>

      <div class="grid-metrics" style="grid-template-columns: repeat(auto-fit, minmax(140px, 1fr))">
        <div>
          <div class="mlabel">flag 进度</div>
          <div class="mval tnum">
            <b>{{ detail.correct_flag_count || 0 }}</b> / {{ detail.flag_count || 0 }}
          </div>
        </div>
        <div>
          <div class="mlabel">分值</div>
          <div class="mval tnum">{{ detail.total_score ?? "—" }}</div>
        </div>
        <div>
          <div class="mlabel">等级</div>
          <div class="mval tnum">{{ detail.level ?? "—" }}</div>
        </div>
        <div>
          <div class="mlabel">容器</div>
          <div class="mval">{{ detail.container_status || "—" }}</div>
        </div>
        <div>
          <div class="mlabel">地址</div>
          <div class="mval mono" style="overflow-wrap: break-word">
            {{ (detail.container_addr || []).join("、") || "—" }}
          </div>
        </div>
      </div>
    </div>

    <!-- 状态带:core dump / 异常终止 / 轮换 / 实时提示(与旧版同级同文案) -->
    <Banner
      v-if="detail.local?.crashed"
      tone="err"
      text="⚠ 目录下有 core dump——上次求解进程崩溃（时间线戛然而止）。"
    />
    <Banner
      v-else-if="meta?.abrupt"
      tone="warn"
      text="⚠ 上次会话异常终止，时间线没有正常结束标记。"
    />
    <Banner
      v-if="meta?.truncated"
      tone="info"
      text="transcript 曾因体积轮换被清空——这里只保留最近一段历史。"
    />
    <Banner
      v-if="liveHere"
      tone="info"
      text="正在实时求解——时间线每 2 秒追加，可随时点开工具行看完整输出。"
    />
    <Banner v-else-if="meta?.live" tone="info" text="时间线随文件追加而增长。" />

    <div class="panel">
      <h2>本地已解 flag</h2>
      <template v-if="(detail.flags || []).length">
        <FlagRow v-for="f in detail.flags" :key="f" :flag="f" />
      </template>
      <div v-else-if="detail.is_completed" class="mut" style="font-size: 13px">
        平台标记已完成，但本机没有 FLAG 文件（旧会话产物，见右侧会话留痕）。
      </div>
      <div v-else class="mut" style="font-size: 13px">本机还没有解出记录。</div>

      <div class="chiprow">
        <Chip
          v-if="detail.local?.transcript_bytes"
          tone="sess"
          :text="`transcript ${fmtMB(detail.local.transcript_bytes)}`"
        />
        <Chip v-if="detail.local?.crashed" tone="crash" text="core dump" />
        <template v-if="(detail.local?.artifacts || []).length">
          <Chip v-for="a in detail.local?.artifacts || []" :key="a" tone="art" :text="a" />
        </template>
        <span v-else class="dim" style="font-size: 12px">（无）</span>
      </div>

      <div class="dim" style="margin-top: 4px; font-size: 12px">
        最后活动:{{ fmtDateTime(detail.local?.last_activity) }}
        <template v-if="detail.local?.transcript_mtime">
          · transcript 更新于 {{ fmtClock((detail.local.transcript_mtime || 0) * 1000) }}
        </template>
      </div>
    </div>

    <div class="panel">
      <h2>
        会话时间线
        <span v-if="meta && meta.sessions != null" class="sub">
          · {{ meta.sessions }} 个会话 · {{ entries.length }} 条摘要{{
            meta.live ? " · 实时追加中" : ""
          }}
        </span>
      </h2>
      <div ref="tlRoot">
        <Timeline v-if="entries.length" :entries="entries" />
        <div v-else class="loading">还没有会话记录——worker 还没碰过这题。</div>
      </div>
    </div>

    <div class="panel">
      <TranscriptRaw :code="code" :tick="rawTick" v-model:open="rawOpen" />
    </div>
  </template>
</template>

<style scoped>
.headline {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 2px;
}
.desccoll {
  --el-collapse-border-color: transparent;
  --el-collapse-header-bg-color: transparent;
  --el-collapse-content-bg-color: transparent;
}
.desc-body {
  font-size: 13.5px;
  white-space: pre-wrap;
  overflow-wrap: break-word;
}
.chiprow {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 10px;
}
</style>
