<script lang="ts">
  import { fetchRoster } from "../lib/api";
  import { fmtRel } from "../lib/format";
  import { goChallenge } from "../lib/route.svelte";
  import type { ChallengeRow, RosterSnapshot } from "../lib/types";
  import Banner from "../components/Banner.svelte";
  import Chip from "../components/Chip.svelte";
  import DiffBadge from "../components/DiffBadge.svelte";
  import StatCard from "../components/StatCard.svelte";

  let roster: RosterSnapshot | null = $state(null);
  let failed = $state(false);
  let sortKey: "code" | "score" | "prog" | "act" | null = $state(null);
  let sortAsc = $state(true);

  // 派生值在模板 else 分支内以 {@const} 计算(roster 已收窄为 RosterSnapshot)

  // ── 排序:与旧版逐字一致的 comparator ──
  function keyOf(x: ChallengeRow, k: "code" | "score" | "prog" | "act"): string | number {
    if (k === "code") return x.unique_code;
    if (k === "score") return x.total_score || 0;
    if (k === "prog") return x.correct_flag_count || 0;
    return x.local?.last_activity || 0;
  }
  function diffRank(d: string): number {
    return d === "easy" ? 0 : d === "hard" ? 2 : 1;
  }
  function sortRows(
    list: ChallengeRow[],
    sortKey: "code" | "score" | "prog" | "act" | null,
    sortAsc: boolean,
  ): ChallengeRow[] {
    return list.slice().sort((a, b) => {
      if (sortKey) {
        const ka = keyOf(a, sortKey);
        const kb = keyOf(b, sortKey);
        if (typeof ka === "string" && typeof kb === "string") {
          const c = ka.localeCompare(kb);
          return sortAsc ? c : -c;
        }
        return sortAsc ? (ka as number) - (kb as number) : (kb as number) - (ka as number);
      }
      if ((a.is_completed ? 1 : 0) !== (b.is_completed ? 1 : 0)) return a.is_completed ? 1 : -1;
      if (diffRank(a.difficulty) !== diffRank(b.difficulty))
        return diffRank(a.difficulty) - diffRank(b.difficulty);
      return (b.total_score || 0) - (a.total_score || 0);
    });
  }

  function setSort(k: "code" | "score" | "prog" | "act"): void {
    if (sortKey === k) sortAsc = !sortAsc;
    else {
      sortKey = k;
      sortAsc = true;
    }
  }

  async function refresh(): Promise<void> {
    try {
      roster = await fetchRoster();
      failed = false;
    } catch {
      failed = true;
    }
  }

  $effect(() => {
    void refresh();
    const iv = setInterval(() => {
      if (!document.hidden) void refresh();
    }, 10000);
    return () => clearInterval(iv);
  });

  const chips = (x: ChallengeRow) => {
    const out: { tone: "flag" | "crash" | "sess" | "art"; text: string }[] = [];
    if (x.local?.flag) out.push({ tone: "flag", text: "FLAG ✓" });
    if (x.local?.crashed) out.push({ tone: "crash", text: "core" });
    if (x.local?.transcript_bytes) out.push({ tone: "sess", text: "会话" });
    const arts = x.local?.artifacts?.length || 0;
    if (arts) out.push({ tone: "art", text: `产物 ${arts}` });
    return out;
  };

  const TH =
    "whitespace-nowrap border-b border-line2 pb-[7px] pt-[7px] pr-2.5 text-left text-[12px] font-semibold text-dim";
</script>

{#if failed}
  <Banner tone="err" text="无法读取题目总览（/api/roster 不可达）。服务是否在运行？" />
{:else if !roster}
  <div class="py-[26px] text-center text-[13.5px] text-dim">加载中…</div>
{:else}
  {@const rows = Object.values(roster.challenges || {}).filter(
    (x): x is ChallengeRow => !!x && !!x.unique_code,
  )}
  {@const n = rows.length}
  {@const comp = rows.filter((x) => x.is_completed).length}
  {@const flagTotal = rows.reduce((a, x) => a + (x.correct_flag_count || 0), 0)}
  {@const localFlags = rows.filter((x) => x.local?.flag).length}
  {@const sorted = sortRows(rows, sortKey, sortAsc)}
  <h1 class="mb-1 text-[18px] font-bold">任务总览</h1>

  <div class="mb-3.5 mt-4 grid gap-2.5 max-[720px]:grid-cols-2 grid-cols-[repeat(auto-fit,minmax(150px,1fr))]">
    <StatCard label="题目总数" value={n} />
    <StatCard label="已完成" value={comp} tone="green" />
    <StatCard label="待解" value={n - comp} />
    <StatCard label="已确认 flag" value={flagTotal} suffix=" 个" />
    <StatCard label="本地留痕（FLAG 文件）" value={localFlags} tone="amber" />
  </div>

  {#if roster.platform_disabled}
    <Banner
      tone="info"
      text="平台轮询已禁用（缺 BENCHMARK_* 或 SDK 不可用）——下表仅含本地有痕迹的题。"
    />
  {:else if roster.platform_error}
    <Banner
      tone="warn"
      text={`平台数据暂不可用：${roster.platform_error}。正在显示 ${fmtRel(roster.fetched_at)}的缓存，本地痕迹照常刷新。`}
    />
  {:else if roster.fetched_at}
    <div class="mb-2 text-[12px] text-dim">平台数据 {fmtRel(roster.fetched_at)}更新 · 共 {n} 题</div>
  {/if}

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <div class="overflow-x-auto">
      <table class="w-full border-collapse text-[13px]">
        <thead>
          <tr>
            <th class={TH}>
              <button type="button" class="cursor-pointer select-none hover:text-ink {sortKey === "code" ? "text-ink" : ""}" onclick={() => setSort("code")}>
                题号{sortKey === "code" ? (sortAsc ? " ▲" : " ▼") : ""}
              </button>
            </th>
            <th class={TH}>难度</th>
            <th class={TH}>
              <button type="button" class="cursor-pointer select-none hover:text-ink {sortKey === "score" ? "text-ink" : ""}" onclick={() => setSort("score")}>
                分值{sortKey === "score" ? (sortAsc ? " ▲" : " ▼") : ""}
              </button>
            </th>
            <th class={TH}>
              <button type="button" class="cursor-pointer select-none hover:text-ink {sortKey === "prog" ? "text-ink" : ""}" onclick={() => setSort("prog")}>
                flag 进度{sortKey === "prog" ? (sortAsc ? " ▲" : " ▼") : ""}
              </button>
            </th>
            <th class={TH}>本地痕迹</th>
            <th class={TH}>
              <button type="button" class="cursor-pointer select-none hover:text-ink {sortKey === "act" ? "text-ink" : ""}" onclick={() => setSort("act")}>
                最近活动{sortKey === "act" ? (sortAsc ? " ▲" : " ▼") : ""}
              </button>
            </th>
            <th class={TH}></th>
          </tr>
        </thead>
        <tbody>
          {#each sorted as x (x.unique_code)}
            {@const c = chips(x)}
            <tr
              class="cursor-pointer border-b border-line align-middle hover:bg-panel2"
              tabindex="0"
              onclick={() => goChallenge(x.unique_code)}
              onkeydown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  goChallenge(x.unique_code);
                }
              }}
            >
              <td class="px-2.5 py-2">
                <span class="whitespace-nowrap font-mono font-semibold {x.is_completed ? "text-green" : ""}">
                  {x.unique_code}
                </span>
                {#if x.local_only}<span class="ml-1 text-[11px] text-amber">仅本地</span>{/if}
              </td>
              <td class="px-2.5 py-2"><DiffBadge difficulty={x.difficulty} /></td>
              <td class="whitespace-nowrap px-2.5 py-2 tabular-nums">{x.total_score ?? "—"}</td>
              <td class="whitespace-nowrap px-2.5 py-2 tabular-nums">
                {x.correct_flag_count || 0}/{x.flag_count || 0}{x.is_completed ? " ✓" : ""}
              </td>
              <td class="px-2.5 py-2">
                {#if c.length}
                  <span class="flex flex-wrap gap-[5px]">
                    {#each c as chip}
                      <Chip tone={chip.tone} text={chip.text} />
                    {/each}
                  </span>
                {:else}
                  <span class="text-[11.5px] text-dim">—</span>
                {/if}
              </td>
              <td class="whitespace-nowrap px-2.5 py-2 text-[12px] text-dim">
                {x.local?.last_activity ? fmtRel(x.local.last_activity) : "—"}
              </td>
              <td class="whitespace-nowrap px-2.5 py-2 text-[12px] text-mut">查看 →</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if !n}
      <div class="py-[26px] text-center text-[13.5px] text-dim">
        还没有题目数据——worker 首次轮询平台后自动出现。
      </div>
    {/if}
  </div>
{/if}
