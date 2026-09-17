<script setup lang="ts">
/**
 * 观测读凭据门禁:读端返回明文 flag 与完整 agent 实录,服务端已不再匿名开放,
 * 故未持凭据时整个态势台无数据可看。此处给出一次输入入口,避免每个视图各自报错。
 *
 * 触发条件:未配置 read token(首屏),或服务端明确拒绝(401/403/503)。
 * 凭据只存 sessionStorage(见 stores/auth.ts),不落 localStorage。
 */
import { computed, ref } from "vue";
import { readAuth, setReadToken } from "../stores/auth";
import { live } from "../stores/live";

const value = ref("");
const error = ref("");

const show = computed(() => !readAuth.value || live.value.conn === "unauthorized");

function submit(): void {
  const t = value.value.trim();
  if (!t) {
    error.value = "请输入观测读 token";
    return;
  }
  error.value = "";
  setReadToken(t);
  value.value = "";
  // 凭据变更会触发 SSE 重连与各视图重新拉取;若仍 401,live.conn 会回到 unauthorized
}
</script>

<template>
  <div v-if="show" class="gate">
    <div class="gate-title">需要观测凭据</div>
    <p class="gate-copy">
      观测读端返回<strong class="c-ink">明文 flag 与完整 agent 实录</strong>,服务端不再匿名开放。
      请输入平台配置的观测读 token(未单独配置 <code>OBSERVABILITY_READ_TOKEN</code> 时即为 ingest
      token)。
    </p>
    <form class="gate-form" @submit.prevent="submit">
      <input
        v-model="value"
        type="password"
        placeholder="观测读 token"
        autocomplete="off"
        class="gate-input mono"
      />
      <el-button native-type="submit">应用</el-button>
    </form>
    <div v-if="error" class="gate-err">{{ error }}</div>
    <div v-else-if="readAuth && live.conn === 'unauthorized'" class="gate-err">
      凭据被服务端拒绝,请确认取值。
    </div>
  </div>
</template>

<style scoped>
.gate {
  margin-bottom: 16px;
  border: 1px solid rgba(232, 163, 61, 0.4);
  border-radius: 8px;
  background: rgba(232, 163, 61, 0.09);
  padding: 12px 16px;
}
.gate-title {
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 500;
  color: var(--amberlight);
}
.gate-copy {
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--dim);
}
.gate-copy code {
  font-family: var(--font-mono);
  color: var(--mut);
}
.gate-form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.gate-input {
  height: 28px;
  min-width: 220px;
  flex: 1;
  border: 1px solid var(--line2);
  border-radius: 6px;
  background: var(--panel2);
  padding: 0 10px;
  font-size: 12.5px;
  color: var(--ink);
  outline: none;
}
.gate-input::placeholder {
  color: var(--dim);
}
.gate-input:focus {
  border-color: var(--blue);
}
.gate-err {
  margin-top: 8px;
  font-size: 12px;
  color: var(--redlight);
}
</style>
