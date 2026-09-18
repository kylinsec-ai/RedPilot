<script lang="ts">
  import { untrack } from "svelte";
  import { ApiError, cancelEvaluation, createEvaluation, fetchEvaluations, fetchWorkers } from "../lib/api";
  import { adminAuth, setAdminToken } from "../lib/auth.svelte";
  import { fmtDateTime } from "../lib/format";
  import type { EvaluationRow, WorkerRow } from "../lib/types";
  import Banner from "../components/Banner.svelte";
  import StatCard from "../components/StatCard.svelte";

  let tokenInput = $state(adminAuth.token);
  let taskToken = $state("");
  let projectId = $state("default");
  let idempotencyKey = $state("");
  let evaluations: EvaluationRow[] = $state([]);
  let workers: WorkerRow[] = $state([]);
  let loading = $state(false);
  let saving = $state(false);
  let error = $state("");

  const authed = $derived(Boolean(adminAuth.token));
  const running = $derived(evaluations.filter((x) => x.status === "running").length);
  const queued = $derived(evaluations.filter((x) => x.status === "queued").length);
  const onlineWorkers = $derived(
    workers.filter((x) => x.status === "idle" || x.status === "busy").length,
  );

  async function load(): Promise<void> {
    if (!adminAuth.token) return;
    loading = true;
    // 两表独立加载:一端 401/502 不丢另一端的旧数据,不再全有全无。
    const [e, w] = await Promise.allSettled([fetchEvaluations(), fetchWorkers()]);
    if (e.status === "fulfilled") evaluations = e.value.evaluations;
    if (w.status === "fulfilled") workers = w.value.workers;
    const failures = [e, w].flatMap((r) => (r.status === "rejected" ? [r.reason] : []));
    error = failures.map((r) => String(r)).join("\n");
    // token 错了留着只会每次同错:清掉并提示重登。
    if (failures.some((r) => r instanceof ApiError && r.status === 401)) resetSession();
    loading = false;
  }

  function resetSession(): void {
    setAdminToken("");
    evaluations = [];
    workers = [];
  }

  function login(): void {
    setAdminToken(tokenInput);
    // 加载由下方 $effect 拥有:此处不再显式 load(原来双发两次 GET)。
  }

  async function submitEvaluation(): Promise<void> {
    if (!taskToken.trim()) return;
    saving = true;
    error = "";
    try {
      await createEvaluation(taskToken.trim(), projectId.trim() || "default", idempotencyKey.trim());
      taskToken = "";
      await load();
    } catch (e) {
      error = String(e);
    } finally {
      saving = false;
    }
  }
  async function cancel(item: EvaluationRow): Promise<void> {
    error = "";
    try {
      await cancelEvaluation(item.evaluation_id);
      await load();
    } catch (e) {
      error = String(e);
    }
  }


  $effect(() => {
    adminAuth.token;
    untrack(() => {
      if (adminAuth.token) void load();
    });
  });
</script>

<h1 class="mb-1 text-[18px] font-bold">控制面</h1>
<p class="mb-4 text-[13px] text-dim">Evaluation、Job 与 Worker 的统一入口。判分仍由 core 控制面负责。</p>

{#if !authed}
  <div class="max-w-[520px] rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <h2 class="mb-2 text-[14px] font-semibold">管理员凭据</h2>
    <p class="mb-3 text-[12.5px] leading-relaxed text-dim">
      凭据只保存在当前浏览器 session，并通过同源代理发送到 control；不会写入任务数据。
    </p>
    <form
      class="flex gap-2"
      onsubmit={(event) => {
        event.preventDefault();
        login();
      }}
    >
      <input
        bind:value={tokenInput}
        type="password"
        autocomplete="current-password"
        placeholder="REDPILOT_ADMIN_TOKEN"
        class="h-8 min-w-0 flex-1 rounded-md border border-line2 bg-panel2 px-2.5 font-mono text-[12.5px] text-ink placeholder:text-dim focus:border-blue focus:outline-none"
      />
      <button
        type="submit"
        class="h-8 rounded-md border border-blue bg-blue/10 px-3.5 text-[13px] text-bluetint hover:bg-blue/20"
      >
        连接
      </button>
    </form>
  </div>
{:else}
  {#if error}<Banner tone="err" text={`控制面请求失败：${error}`} />{/if}

  <div class="mb-3.5 grid gap-2.5 max-[720px]:grid-cols-2 grid-cols-[repeat(auto-fit,minmax(150px,1fr))]">
    <StatCard label="Evaluation 总数" value={evaluations.length} />
    <StatCard label="排队中" value={queued} tone="amber" />
    <StatCard label="运行中" value={running} tone="green" />
    <StatCard label="在线 Worker" value={onlineWorkers} />
  </div>

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <div class="mb-3 flex items-center justify-between gap-3">
      <h2 class="text-[14px] font-semibold">创建 Evaluation</h2>
      <button
        type="button"
        class="text-[12px] text-dim hover:text-ink"
        onclick={resetSession}
      >
        断开凭据
      </button>
    </div>
    <form
      class="flex flex-wrap gap-2"
      onsubmit={(event) => {
        event.preventDefault();
        void submitEvaluation();
      }}
    >
      <input
        bind:value={taskToken}
        placeholder="task token"
        class="h-8 min-w-[220px] flex-1 rounded-md border border-line2 bg-panel2 px-2.5 font-mono text-[12.5px] text-ink placeholder:text-dim focus:border-blue focus:outline-none"
      />
      <input
        bind:value={projectId}
        placeholder="project"
        class="h-8 w-32 rounded-md border border-line2 bg-panel2 px-2.5 font-mono text-[12.5px] text-ink placeholder:text-dim focus:border-blue focus:outline-none"
      />
      <input
        bind:value={idempotencyKey}
        placeholder="幂等 key（可选）"
        class="h-8 w-40 rounded-md border border-line2 bg-panel2 px-2.5 font-mono text-[12.5px] text-ink placeholder:text-dim focus:border-blue focus:outline-none"
      />
      <button
        type="submit"
        disabled={saving || !taskToken.trim()}
        class="h-8 rounded-md border border-blue bg-blue/10 px-3.5 text-[13px] text-bluetint enabled:hover:bg-blue/20 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {saving ? "创建中…" : "创建"}
      </button>
    </form>
  </div>

  <div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <div class="mb-2 flex items-center justify-between">
      <h2 class="text-[14px] font-semibold">Evaluations</h2>
      <button type="button" class="text-[12px] text-dim hover:text-ink" onclick={() => void load()}>
        {loading ? "刷新中…" : "刷新"}
      </button>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full border-collapse text-[13px]">
        <thead>
          <tr class="text-left text-[12px] font-semibold text-dim">
            <th class="border-b border-line2 pb-2 pr-3">Evaluation</th>
            <th class="border-b border-line2 pb-2 pr-3">Project</th>
            <th class="border-b border-line2 pb-2 pr-3">状态</th>
            <th class="border-b border-line2 pb-2 pr-3">Job 完成/总数</th>
            <th class="border-b border-line2 pb-2 pr-3">待/跑/败</th>
            <th class="border-b border-line2 pb-2 pr-3">创建时间</th>
            <th class="border-b border-line2 pb-2">操作</th>
          </tr>
        </thead>
        <tbody>
          {#each evaluations as item (item.evaluation_id)}
            <tr class="border-b border-line align-middle">
              <td class="px-1 py-2 font-mono text-[12px]">{item.evaluation_id.slice(0, 12)}…</td>
              <td class="px-1 py-2 font-mono text-[12px] text-mut">{item.project_id}</td>
              <td class="px-1 py-2">{item.status}</td>
              <td class="px-1 py-2 tabular-nums text-mut">
                {item.completed_count}/{item.job_count}
              </td>
              <td class="px-1 py-2 tabular-nums text-[12px] text-mut">
                {item.pending_count}/{item.running_count}/{item.failed_count}
              </td>
              <td class="px-1 py-2 text-[12px] text-mut">{fmtDateTime(item.created_at)}</td>
              <td class="px-1 py-2">
                {#if item.status === "queued" || item.status === "running"}
                  <button
                    type="button"
                    class="text-[12px] text-dim hover:text-ink"
                    onclick={() => void cancel(item)}
                  >
                    取消
                  </button>
                {:else}—{/if}
              </td>
            </tr>
          {:else}
            <tr><td colspan="7" class="py-5 text-center text-dim">暂无 Evaluation</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>

  <div class="rounded-[10px] border border-line bg-panel px-4.5 py-4">
    <h2 class="mb-2 text-[14px] font-semibold">Workers</h2>
    <div class="overflow-x-auto">
      <table class="w-full border-collapse text-[13px]">
        <thead>
          <tr class="text-left text-[12px] font-semibold text-dim">
            <th class="border-b border-line2 pb-2 pr-3">Worker</th>
            <th class="border-b border-line2 pb-2 pr-3">状态</th>
            <th class="border-b border-line2 pb-2 pr-3">能力</th>
            <th class="border-b border-line2 pb-2">最近心跳</th>
          </tr>
        </thead>
        <tbody>
          {#each workers as item (item.worker_id)}
            <tr class="border-b border-line align-middle">
              <td class="px-1 py-2 font-mono text-[12px]">{item.worker_id}</td>
              <td class="px-1 py-2">{item.status}</td>
              <td class="px-1 py-2 font-mono text-[11.5px] text-mut">
                {Object.keys(item.capabilities).join(", ") || "—"}
              </td>
              <td class="px-1 py-2 text-[12px] text-mut">{fmtDateTime(item.last_seen_at)}</td>
            </tr>
          {:else}
            <tr><td colspan="4" class="py-5 text-center text-dim">暂无 Worker</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>
{/if}
