<script lang="ts">
  import { fetchChallenge, fetchTimeline } from "../lib/api";
  import { fmtMB } from "../lib/format";
  import { live } from "../lib/live.svelte";
  import { ACTIVE_PHASES } from "../lib/types";
  import type {
    ChallengeDetail,
    TimelineEntry,
    TimelineMeta,
  } from "../lib/types";
  import Banner from "../components/Banner.svelte";
  import Chip from "../components/Chip.svelte";
  import DiffBadge from "../components/DiffBadge.svelte";
  import FlagRow from "../components/FlagRow.svelte";
  import Timeline from "../components/detail/Timeline.svelte";
  import TranscriptRaw from "../components/detail/TranscriptRaw.svelte";

  let { code }: { code: string } = $props();

  let detail: ChallengeDetail | null = $state(null);
  let fatal: string | null = $state(null);
  let meta: TimelineMeta | null = $state(null);
  let entries: TimelineEntry[] = $state([]);
  let nextSeq = $state(0);
  let rawOpen = $state(false);
  let rawTick = $state(0);
  let tlRoot: HTMLDivElement | null = $state(null);

  const liveHere = $derived(
    ACTIVE_PHASES.includes(live.phase) && live.challenge_code === code,
  );

  async function refresh(c: string): Promise<void> {
    try {
      const ch = await fetchChallenge(c);
      detail = ch;
      let tl = await fetchTimeline(c, nextSeq);
      if (tl.meta?.truncated) {
        // worker 侧 digest 被重截断(压缩/5MB 轮换):seq 从头编号,本地旧条目作废
        // (keyed each 会与重编的 seq 冲突/叠行)—— 先取到全量再替换,失败则保留旧条目
        const fresh = await fetchTimeline(c, 0);
        entries.length = 0;
        nextSeq = 0;
        tl = fresh;
      }
      meta = tl.meta;
      for (const en of tl.entries) entries.push(en); // append-only:keyed 行展开态不丢
      nextSeq = tl.next_seq;
      fatal = null;
    } catch (e) {
      // 有旧数据时保持现状续轮询;首屏失败记 fatal(修复:旧版此处轮询永久停)
      if (!detail) fatal = String(e);
    }
    rawTick += 1; // raw details 开着时随轮询刷新,关闭时 TranscriptRaw 自行忽略
  }

  /**
   * 自调度轮询:每轮结束后按当前相位定下次间隔(本页求解中 2s / 其余 4s)。
   * 瞬时失败与后台 tick 不终止循环(修复);切后台跳过 fetch,回前台恢复。
   */
  $effect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      if (stopped) return;
      if (!document.hidden) await refresh(code);
      if (stopped) return;
      const delay = liveHere ? 2000 : 4000;
      timer = setTimeout(() => {
        void tick();
      }, delay);
    };
    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  });

  /** 时间线追加后:仅当近底部且未开 raw、页面可见时跟随滚动,绝不拽走正在读旧内容的用户 */
  $effect(() => {
    if (document.hidden || !liveHere || rawOpen) return;
    entries.length; // 依赖:有新条目追加才评估
    const el = tlRoot;
    if (!el) return;
    const near = el.getBoundingClientRect().bottom - window.innerHeight < 320;
    if (near) el.scrollIntoView({ block: "end" });
  });
</script>

{#if fatal}
  <a class="mb-3 mt-1 inline-block text-[13px]" href="#/">← 返回题目总览</a>
  <Banner tone="err" text={`加载失败：${fatal}`} />
{:else if !detail}
  <a class="mb-3 mt-1 inline-block text-[13px]" href="#/">← 返回题目总览</a>
  <div class="py-[26px] text-center text-[13.5px] text-dim">加载题目 {code}…</div>
{:else}
  {@const d = detail}
  {@const l = d.local}
  {@const flags = d.flags || []}
  {@const arts = l?.artifacts || []}
  <a class="mb-3 mt-1 inline-block text-[13px]" href="#/">← 返回题目总览</a>

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <div class="mb-0.5 flex flex-wrap items-center gap-3">
      <h1 class="font-mono text-[12.5px] font-bold">{d.unique_code}</h1>
      <DiffBadge difficulty={d.difficulty} />
      {#if liveHere}<Chip tone="live" text="● 求解中" />{/if}
      {#if d.is_completed}<Chip tone="flag" text="已完成" />{/if}
      {#if d.local_only}<span class="text-[11px] text-amber">仅本地痕迹</span>{/if}
    </div>
    {#if d.description}
      <details class="desc">
        <summary class="cursor-pointer py-0.5 text-[13px] text-blue select-none">
          题目描述
        </summary>
        <div class="mt-2 text-[13.5px] whitespace-pre-wrap break-words text-mut">
          {d.description}
        </div>
      </details>
    {/if}
    <div class="mt-3 grid gap-2.5 grid-cols-[repeat(auto-fit,minmax(140px,1fr))]">
      <div>
        <div class="text-[11.5px] text-dim">flag 进度</div>
        <div class="mt-px text-[13.5px] tabular-nums"><b>{d.correct_flag_count || 0}</b> / {d.flag_count || 0}</div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">分值</div>
        <div class="mt-px text-[13.5px] tabular-nums">{d.total_score ?? "—"}</div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">等级</div>
        <div class="mt-px text-[13.5px] tabular-nums">{d.level ?? "—"}</div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">容器</div>
        <div class="mt-px text-[13.5px]">{d.container_status || "—"}</div>
      </div>
      <div>
        <div class="text-[11.5px] text-dim">地址</div>
        <div class="mt-px break-all font-mono text-[13.5px]">
          {(d.container_addr || []).join("、") || "—"}
        </div>
      </div>
    </div>
  </div>

  <!-- 状态带:core dump / 异常终止 / 轮换 / 实时提示(与旧版同级同文案) -->
  {#if l?.crashed}
    <Banner tone="err" text="⚠ 目录下有 core dump——上次求解进程崩溃（时间线戛然而止）。" />
  {:else if meta?.abrupt}
    <Banner tone="warn" text="⚠ 上次会话异常终止，时间线没有正常结束标记。" />
  {/if}
  {#if meta?.truncated}
    <Banner tone="info" text="transcript 曾因体积轮换被清空——这里只保留最近一段历史。" />
  {/if}
  {#if liveHere}
    <Banner tone="info" text="正在实时求解——时间线每 2 秒追加，可随时点开工具行看完整输出。" />
  {:else if meta?.live}
    <Banner tone="info" text="时间线随文件追加而增长。" />
  {/if}

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <h2 class="mb-2.5 text-[14px] font-semibold">本地已解 flag</h2>
    {#if flags.length}
      {#each flags as f (f)}
        <FlagRow flag={f} />
      {/each}
    {:else if d.is_completed}
      <div class="text-[13px] text-mut">
        平台标记已完成，但本机没有 FLAG 文件（旧会话产物，见右侧会话留痕）。
      </div>
    {:else}
      <div class="text-[13px] text-mut">本机还没有解出记录。</div>
    {/if}
    <div class="mt-2.5 flex flex-wrap gap-[5px]">
      {#if l?.transcript_bytes}<Chip tone="sess" text={`transcript ${fmtMB(l.transcript_bytes)}`} />{/if}
      {#if l?.crashed}<Chip tone="crash" text="core dump" />{/if}
      {#if arts.length}
        {#each arts as a}
          <Chip tone="art" text={a} />
        {/each}
      {:else}
        <span class="text-[12px] text-dim">（无）</span>
      {/if}
    </div>
    <div class="mt-1 text-[12px] text-dim">
      最后活动:{l?.last_activity
        ? new Date(l.last_activity * 1000).toLocaleString()
        : "—"}
      {#if l?.transcript_mtime}
        · transcript 更新于 {new Date(l.transcript_mtime * 1000).toLocaleTimeString()}
      {/if}
    </div>
  </div>

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <h2 class="mb-2.5 text-[14px] font-semibold">
      会话时间线
      {#if meta && meta.sessions != null}
        <span class="text-[12px] font-normal text-dim">
          · {meta.sessions} 个会话 · {entries.length} 条摘要{meta.live ? " · 实时追加中" : ""}
        </span>
      {/if}
    </h2>
    <div class="tl" bind:this={tlRoot}>
      {#if entries.length}
        <Timeline entries={entries} />
      {:else}
        <div class="py-[26px] text-center text-[13.5px] text-dim">
          还没有会话记录——worker 还没碰过这题。
        </div>
      {/if}
    </div>
  </div>

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <TranscriptRaw {code} tick={rawTick} bind:open={rawOpen} />
  </div>
{/if}
