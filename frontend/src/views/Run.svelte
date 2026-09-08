<script lang="ts">
  import {
    fetchRun,
    fetchRunEvents,
    fetchRunTimeline,
  } from "../lib/api";
  import { goChallenge } from "../lib/route.svelte";
  import { RUN_STATUS_LABEL } from "../lib/types";
  import type {
    RunEventRow,
    RunEventsResp,
    RunRow,
    TimelineEntry,
    TimelineMeta,
  } from "../lib/types";
  import Banner from "../components/Banner.svelte";
  import Chip from "../components/Chip.svelte";
  import FlagRow from "../components/FlagRow.svelte";
  import Timeline from "../components/detail/Timeline.svelte";

  let { runId }: { runId: string } = $props();

  let detail: RunRow | null = $state(null);
  let fatal = $state("");
  // 折叠时间线(append-only,与 Challenge 同法)
  let entries: TimelineEntry[] = $state([]);
  let meta: TimelineMeta | null = $state(null);
  let nextSeq = $state(0);
  // 原始事件分页日志
  let evRows: RunEventRow[] = $state([]);
  let evAfter = $state(0);
  let evEnd = $state(false);
  let evLoading = $state(false);
  let evLoaded = $state(false);

  const STATUS_CLS: Record<string, string> = {
    running: "text-amberlight border-[rgba(232,163,61,0.5)]",
    solved: "text-green border-[rgba(53,192,122,0.5)]",
    done: "text-ink border-line2",
    failed: "text-red border-[rgba(229,83,75,0.55)]",
    interrupted: "text-dim border-line2",
  };

  function fmtSec(ts?: number | null): string {
    return ts
      ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false })
      : "—";
  }

  async function loadTimeline(): Promise<void> {
    const tl = await fetchRunTimeline(runId, nextSeq);
    meta = tl.meta;
    for (const en of tl.entries) entries.push(en);
    nextSeq = tl.next_seq;
  }

  async function refresh(): Promise<void> {
    const d = await fetchRun(detail ? detail.run_id : runId);
    detail = d;
    await loadTimeline();
    if (!evLoaded) void loadMoreEvents(); // 首帧即拉第一页原始事件
  }

  /**
   * 自调度:running 每 4s 追一次(事件随 worker 推送增长),终态单次即止。
   * 只依赖 runId(run 切换时由 {#key} 重建本组件):detail/entries 等更新
   * 不再重跑 effect —— 否则每次刷新都会 cancel 计时器立刻再刷,退化成忙轮询。
   * 首次刷新失败(detail 仍 null)时 2s 重试:瞬时失败不杀死轮询(与 Challenge 同法)。
   */
  $effect(() => {
    runId;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      try {
        if (!document.hidden) {
          await refresh();
          fatal = "";
        }
        // 切后台跳过 fetch 时不碰 fatal:旧报错留到下次成功刷新再清
      } catch (e) {
        if (!detail) fatal = String(e);
      }
      if (stopped) return;
      // async 之后的读取不构成 effect 依赖:由链式计时器自己决定是否续期
      const s = detail?.status;
      if (s === "running") timer = setTimeout(() => void tick(), 4000);
      else if (s === undefined) timer = setTimeout(() => void tick(), 2000);
    };
    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  });

  async function loadMoreEvents(): Promise<void> {
    if (evLoading || evEnd) return;
    evLoading = true;
    try {
      const r: RunEventsResp = await fetchRunEvents(runId, evAfter, 500);
      for (const e of r.events) evRows.push(e);
      evAfter = r.next_seq;
      evEnd = r.end;
      evLoaded = true;
    } catch (e) {
      fatal = String(e);
    } finally {
      evLoading = false;
    }
  }
</script>

{#if fatal && !detail}
  <a class="mb-3 mt-1 inline-block text-[13px]" href="#/runs">← 返回 Runs 历史</a>
  <Banner tone="err" text={`加载失败：${fatal}`} />
{:else if !detail}
  <a class="mb-3 mt-1 inline-block text-[13px]" href="#/runs">← 返回 Runs 历史</a>
  <div class="py-[26px] text-center text-[13.5px] text-dim">加载 run {runId}…</div>
{:else}
  {@const d = detail}
  {@const flags = d.flags_accepted || []}
  <a class="mb-3 mt-1 inline-block text-[13px]" href="#/runs">← 返回 Runs 历史</a>

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <div class="mb-0.5 flex flex-wrap items-center gap-2.5">
      <h1 class="font-mono text-[12.5px] font-bold">run {d.run_id.slice(0, 12)}…</h1>
      <span
        class={`inline-flex rounded border px-[7px] py-px font-mono text-[11.5px] ${STATUS_CLS[d.status] ?? "text-mut border-line2"}`}
      >
        {RUN_STATUS_LABEL[d.status] ?? d.status}
      </span>
      {#if d.status === "running"}
        <Chip tone="live" text="● 求解中 · 数据随推送增长" />
      {/if}
    </div>
    <div
      class="mt-1 text-[11.5px] text-dim"
      title={d.run_id}
    >{d.run_id}</div>
    <div class="mt-3 grid gap-x-6 gap-y-2.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))]">
      <div>
        <div class="text-[11.5px] text-dim">题目</div>
        <div class="mt-px text-[13.5px]">
          <button
            type="button"
            class="cursor-pointer font-mono font-semibold text-blue underline-offset-2 hover:underline"
            onclick={() => goChallenge(d.challenge_code)}
          >
            {d.challenge_code} ↗
          </button>
        </div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">worker</div>
        <div class="mt-px font-mono text-[13.5px]">{d.worker_id}</div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">模型</div>
        <div class="mt-px break-all font-mono text-[13.5px]">{d.model || "—"}</div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">开始</div>
        <div class="mt-px text-[13.5px] tabular-nums">{fmtSec(d.started_at)}</div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">结束</div>
        <div class="mt-px text-[13.5px] tabular-nums">
          {d.ended_at ? fmtSec(d.ended_at) : d.status === "running" ? "…" : "—"}
        </div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">时长</div>
        <div class="mt-px text-[13.5px] tabular-nums">
          {d.duration_s != null ? d.duration_s.toFixed(1) + " s" : "—"}
        </div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">轮次 / 会话</div>
        <div class="mt-px text-[13.5px] tabular-nums">
          {d.turns ?? "—"} / {d.sessions ?? "—"}
        </div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">发现 flag / 事件行</div>
        <div class="mt-px text-[13.5px] tabular-nums">
          {d.flags_found ?? 0} / {d.event_count}
        </div>
      </div>
    </div>
  </div>

  {#if d.error}
    <Banner
      tone={d.status === "failed" ? "err" : "warn"}
      text={`错误：${d.error}`}
    />
  {/if}
  {#if d.status === "interrupted"}
    <Banner tone="warn" text="此 run 被中断（心跳超时 / worker 换题 / 重启残留）——时间线可能不全。" />
  {/if}

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <h2 class="mb-2.5 text-[14px] font-semibold">
      解出 flag
      {#if flags.length}
        <span class="text-[12px] font-normal text-dim">· {flags.length} 个</span>
      {/if}
    </h2>
    {#if flags.length}
      {#each flags as f (f)}
        <FlagRow flag={f} />
      {/each}
    {:else}
      <div class="text-[13px] text-mut">
        {d.status === "solved" ? "平台侧已判解出，但 FLAG 明文未回传（旧会话或异常路径）。" : "此 run 未解出任何 flag。"}
      </div>
    {/if}
  </div>

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <h2 class="mb-2.5 text-[14px] font-semibold">
      时间线
      {#if meta && meta.sessions != null}
        <span class="text-[12px] font-normal text-dim">
          · {meta.sessions} 个会话 · {entries.length} 条摘要{d.status === "running" ? " · 推送追加中" : ""}
        </span>
      {/if}
    </h2>
    {#if entries.length}
      <Timeline entries={entries} />
    {:else}
      <div class="py-[26px] text-center text-[13.5px] text-dim">
        还没有事件记录——事件由 worker 推送落库后这里出现。
      </div>
    {/if}
  </div>

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <h2 class="mb-2.5 text-[14px] font-semibold">
      原始事件
      <span class="text-[12px] font-normal text-dim">· {evRows.length}{evEnd ? "" : "+"} 行</span>
    </h2>
    <div class="font-mono text-[11px]">
      {#each evRows as e (e.seq)}
        <details class="border-b border-line/70">
          <summary
            class="flex cursor-pointer select-none items-baseline gap-2.5 py-1.5 text-[12px] hover:bg-panel2/50"
          >
            <span class="flex-none tabular-nums text-dim">{e.seq}</span>
            <span class="flex-none rounded border border-line2 px-[6px] py-px text-[10.5px] text-cyan">
              {e.type}
            </span>
          </summary>
          <pre
            class="mb-2 ml-1 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-md bg-panel2/60 px-2.5 py-2 leading-relaxed text-mut"
          >{e.payload}</pre>
        </details>
      {/each}
    </div>
    {#if !evLoaded}
      <button
        type="button"
        class="mt-2 cursor-pointer rounded-md border border-line2 bg-transparent px-3 py-1.5 text-[13px] text-mut hover:border-blue hover:text-ink"
        onclick={() => void loadMoreEvents()}
      >
        加载事件…
      </button>
    {:else if !evEnd}
      <button
        type="button"
        class="mt-2 cursor-pointer rounded-md border border-line2 bg-transparent px-3 py-1.5 text-[13px] text-mut hover:border-blue hover:text-ink"
        onclick={() => void loadMoreEvents()}
        disabled={evLoading}
      >
        {evLoading ? "加载中…" : "加载更多"}
      </button>
    {/if}
  </div>
{/if}
