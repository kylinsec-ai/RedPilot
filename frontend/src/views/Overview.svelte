<script lang="ts">
  import { fmtRel } from "../lib/format";
  import { readAuth } from "../lib/auth.svelte";
  import { goChallenge } from "../lib/route.svelte";
  import { roster, sortRows, toRows, traceChips, refreshRoster } from "../lib/roster.svelte";
  import type { RowSortKey } from "../lib/roster.svelte";
  import Banner from "../components/Banner.svelte";
  import Chip from "../components/Chip.svelte";
  import DiffBadge from "../components/DiffBadge.svelte";
  import StatCard from "../components/StatCard.svelte";

  // 花名册来自共享 store(App 启动时已挂单轮询 10s);挂载时补刷一次,恢复旧版"回总览即刷新"
  const snap = $derived(roster.data);

  // 挂载即补刷(共享节拍最长 10s 旧数据);无依赖,仅挂载跑一次
  // 依赖读凭据:用户在门禁处补录 token 后必须立即重拉 —— 否则停留在挂载时那次
  // 401 的错误态,直到手动刷新或 10s 轮询才恢复(实测过)。
  $effect(() => {
    readAuth.token;
    void refreshRoster();
  });

  let sortKey: RowSortKey | null = $state(null);
  let sortAsc = $state(true);

  function setSort(k: RowSortKey): void {
    if (sortKey === k) sortAsc = !sortAsc;
    else {
      sortKey = k;
      sortAsc = true;
    }
  }

  const TH =
    "whitespace-nowrap border-b border-line2 pb-[7px] pt-[7px] pr-2.5 text-left text-[12px] font-semibold text-dim";
</script>

{#if roster.failed && !snap}
  <Banner tone="err" text="无法读取题目总览（/api/roster 不可达）。服务是否在运行？" />
{:else if !snap}
  <div class="py-[26px] text-center text-[13.5px] text-dim">加载中…</div>
{:else}
  {#if roster.failed}
    <Banner tone="warn" text="题目总览刷新失败，正在显示缓存数据，自动重试中…" />
  {/if}
  {@const rows = toRows(snap)}
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

  {#if snap.platform_disabled}
    <Banner
      tone="info"
      text="平台轮询已禁用（缺 BENCHMARK_* 或 SDK 不可用）——下表仅含本地有痕迹的题。"
    />
  {:else if snap.platform_error}
    <Banner
      tone="warn"
      text={`平台数据暂不可用：${snap.platform_error}。正在显示 ${fmtRel(snap.fetched_at)}的缓存，本地痕迹照常刷新。`}
    />
  {:else if snap.fetched_at}
    <div class="mb-2 text-[12px] text-dim">平台数据 {fmtRel(snap.fetched_at)}更新 · 共 {n} 题</div>
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
            {@const c = traceChips(x.local)}
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
