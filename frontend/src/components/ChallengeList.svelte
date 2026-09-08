<script lang="ts">
  import ChallengeRow from "./ChallengeRow.svelte";
  import {
    DIFF_ORDER,
    diffGroupLabel,
    diffKey,
    roster,
    sortRows,
    toRows,
  } from "../lib/roster.svelte";
  import type { DiffKey } from "../lib/roster.svelte";
  import type { ChallengeRow as Row } from "../lib/types";

  // 激活题号由外壳传入(路由输入显式化);花名册仍读共享单例
  let { activeCode = null }: { activeCode?: string | null } = $props();

  const reduceMq = window.matchMedia("(prefers-reduced-motion: reduce)");

  // 搜索题号子串;不编码进 hash——列表状态由常驻挂载自然保留
  let query = $state("");
  let listEl: HTMLDivElement | null = $state(null);

  const q = $derived(query.trim().toLowerCase());
  const all = $derived(toRows(roster.data));
  const nTotal = $derived(all.length);
  // q 过滤后的行(列表与 reveal 逻辑共用一份,避免两处 filter 语义漂移)
  const filtered = $derived(
    q ? all.filter((r) => r.unique_code.toLowerCase().includes(q)) : all,
  );

  // 先全局默认排序再按难度装桶(桶内相对顺序与逐桶排序一致,sort stable);
  // 未知难度归"其他",组序 DIFF_ORDER
  const groups = $derived.by(() => {
    const sorted = sortRows(filtered);
    const buckets = new Map<DiffKey, Row[]>();
    for (const r of sorted) {
      const k = diffKey(r.difficulty);
      const arr = buckets.get(k) ?? [];
      arr.push(r);
      buckets.set(k, arr);
    }
    const out: { key: DiffKey; label: string; rows: Row[] }[] = [];
    for (const k of DIFF_ORDER) {
      const rows = buckets.get(k);
      if (rows?.length) {
        out.push({ key: k, label: diffGroupLabel(k), rows });
      }
    }
    return out;
  });

  // 激活行 reveal:深链/数据到达/过滤变化时滚到该行。
  // lastScrolled 是副作用记录(非渲染输入)→ 普通 let,防 10s 轮询每次拽滚
  let lastScrolled = "";
  $effect(() => {
    const c = activeCode;
    void q;
    void nTotal;
    void roster.data; // 快照本体:同数量内容置换(增激活行/减他行)也重跑
    if (!c) {
      lastScrolled = ""; // 回到总览即清,重访同题仍 reveal
      return;
    }
    if (c === lastScrolled || document.hidden) return;
    const present = all.some((r) => r.unique_code === c);
    if (present && !filtered.some((r) => r.unique_code === c)) {
      // 搜索吞掉了激活行 → 清空搜索,下一轮 effect 再滚
      query = "";
      return;
    }
    if (!present) {
      // 全量花名册都没有该题(平台断连/历史题深链):清搜索也滚不到,
      // 此时绝不能碰 query —— 否则每次击键都会被本 effect 抹掉,搜索框废掉
      return;
    }
    const el = listEl?.querySelector<HTMLElement>(`[data-code="${c}"]`);
    if (!el) return; // 行尚未渲染(数据未到)→ 不记 lastScrolled,数据到达后重试
    el.scrollIntoView({ block: "nearest", behavior: reduceMq.matches ? "auto" : "smooth" });
    lastScrolled = c;
  });
</script>

<div class="px-2 pb-1.5">
  <input
    bind:value={query}
    type="search"
    placeholder="搜索题号…"
    aria-label="搜索题目"
    data-autofocus
    class="w-full rounded-md border border-line2 bg-bg px-2.5 py-1 text-[12.5px] text-ink outline-none placeholder:text-dim focus:border-blue"
    onkeydown={(e) => {
      // 仅清搜索,不冒泡——App 抽屉 Esc 关抽屉,一次按键只做一件事
      if (e.key === "Escape") {
        e.stopPropagation();
        query = "";
      }
    }}
  />
</div>

{#if roster.failed && !roster.data}
  <div class="px-3.5 py-2 text-[12px] text-dim">花名册暂不可用，自动重试中…</div>
{:else if !roster.data}
  <div class="px-3.5 py-2 text-[12px] text-dim">加载中…</div>
{:else if nTotal === 0}
  <div class="px-3.5 py-2 text-[12px] text-dim">还没有题目数据</div>
{:else if groups.length === 0}
  <div class="px-3.5 py-2 text-[12px] text-dim">无匹配题目</div>
{:else}
  <div bind:this={listEl} class="space-y-3">
    {#each groups as grp (grp.key)}
      <div>
        <div class="flex items-baseline justify-between px-2.5 pb-1">
          <span class="text-[11px] font-semibold text-dim">{grp.label}</span>
          <span class="text-[10.5px] tabular-nums text-dim/70">{grp.rows.length}</span>
        </div>
        <div class="space-y-[2px]">
          {#each grp.rows as r (r.unique_code)}
            <ChallengeRow row={r} active={r.unique_code === activeCode} />
          {/each}
        </div>
      </div>
    {/each}
  </div>
{/if}
