<script lang="ts">
  /**
   * 观测读凭据门禁:读端返回明文 flag 与完整 agent 实录,服务端已不再匿名开放,
   * 故未持凭据时整个态势台无数据可看。此处给出一次输入入口,避免每个视图各自报错。
   *
   * 触发条件:未配置 read token(首屏),或服务端明确拒绝(401/403/503)。
   * 凭据只存 sessionStorage(见 auth.svelte.ts),不落 localStorage。
   */
  import { readAuth, setReadToken } from "../lib/auth.svelte";
  import { live } from "../lib/live.svelte";

  let value = $state("");
  let error = $state("");

  const show = $derived(!readAuth.token || live.conn === "unauthorized");

  function submit(): void {
    const t = value.trim();
    if (!t) {
      error = "请输入观测读 token";
      return;
    }
    error = "";
    setReadToken(t);
    value = "";
    // 凭据变更会触发 SSE 重连与各视图重新拉取;若仍 401,live.conn 会回到 unauthorized
  }
</script>

{#if show}
  <div
    class="mb-4 rounded-lg border border-[rgba(232,163,61,0.4)] bg-[rgba(232,163,61,0.09)] px-4 py-3"
  >
    <div class="mb-2 text-[13px] font-medium text-amberlight">需要观测凭据</div>
    <p class="mb-3 text-[12.5px] leading-relaxed text-mid">
      观测读端返回<strong class="text-high">明文 flag 与完整 agent 实录</strong>,服务端不再匿名开放。
      请输入平台配置的观测读 token(未单独配置 <code>OBSERVABILITY_READ_TOKEN</code> 时即为 ingest token)。
    </p>
    <form class="flex flex-wrap items-center gap-2" onsubmit={(e) => (e.preventDefault(), submit())}>
      <input
        type="password"
        bind:value
        placeholder="观测读 token"
        autocomplete="off"
        class="min-w-[220px] flex-1 rounded-md border border-linebg bg-panel px-2.5 py-1.5 text-[13px] text-high outline-none focus:border-bluetint"
      />
      <button
        type="submit"
        class="rounded-md border border-linebg bg-panel px-3 py-1.5 text-[13px] text-high hover:border-bluetint"
      >
        应用
      </button>
    </form>
    {#if error}
      <div class="mt-2 text-[12px] text-redlight">{error}</div>
    {:else if readAuth.token && live.conn === "unauthorized"}
      <div class="mt-2 text-[12px] text-redlight">凭据被服务端拒绝,请确认取值。</div>
    {/if}
  </div>
{/if}
