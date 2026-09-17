<script setup lang="ts">
/**
 * 控制面:Evaluation / Job / Worker 的统一入口(判分仍在 core 控制面)。
 * 凭据只存在当前浏览器 session,并经同源代理发给 control —— 见 stores/auth.ts。
 *
 * 两处刻意的行为(与旧版一致):
 *  · 两张表**独立加载**(allSettled):一端 401/502 不该丢另一端的旧数据。
 *  · 401 即清会话:token 错了留着只会每次同错,直接退回登录表单。
 * 本视图不轮询 —— 只有"刷新"按钮与凭据变化触发。
 */
import { computed, onMounted, ref, watch } from "vue";
import { ApiError } from "../api/client";
import { cancelEvaluation, createEvaluation, fetchEvaluations, fetchWorkers } from "../api/control";
import { fmtDateTime } from "../lib/format";
import { adminAuth, setAdminToken } from "../stores/auth";
import type { EvaluationRow, WorkerRow } from "../types";
import Banner from "../components/Banner.vue";
import StatCard from "../components/StatCard.vue";

const tokenInput = ref(adminAuth.value);
const taskToken = ref("");
const projectId = ref("default");
const idempotencyKey = ref("");
const evaluations = ref<EvaluationRow[]>([]);
const workers = ref<WorkerRow[]>([]);
const loading = ref(false);
const saving = ref(false);
const error = ref("");

const authed = computed(() => Boolean(adminAuth.value));
const running = computed(() => evaluations.value.filter((x) => x.status === "running").length);
const queued = computed(() => evaluations.value.filter((x) => x.status === "queued").length);
const onlineWorkers = computed(
  () => workers.value.filter((x) => x.status === "idle" || x.status === "busy").length,
);

async function load(): Promise<void> {
  if (!adminAuth.value) return;
  loading.value = true;
  const [e, w] = await Promise.allSettled([fetchEvaluations(), fetchWorkers()]);
  if (e.status === "fulfilled") evaluations.value = e.value.evaluations;
  if (w.status === "fulfilled") workers.value = w.value.workers;
  const failures = [e, w].flatMap((r) => (r.status === "rejected" ? [r.reason] : []));
  error.value = failures.map((r) => String(r)).join("\n");
  if (failures.some((r) => r instanceof ApiError && r.status === 401)) resetSession();
  loading.value = false;
}

function resetSession(): void {
  setAdminToken("");
  evaluations.value = [];
  workers.value = [];
}

/** 登录只负责写凭据;加载由 watch 拥有(旧版此处双发过两次 GET)。 */
function login(): void {
  setAdminToken(tokenInput.value);
}

async function submitEvaluation(): Promise<void> {
  if (!taskToken.value.trim()) return;
  saving.value = true;
  error.value = "";
  try {
    await createEvaluation(
      taskToken.value.trim(),
      projectId.value.trim() || "default",
      idempotencyKey.value.trim(),
    );
    taskToken.value = "";
    await load();
  } catch (e) {
    error.value = String(e);
  } finally {
    saving.value = false;
  }
}

async function cancel(item: EvaluationRow): Promise<void> {
  error.value = "";
  try {
    await cancelEvaluation(item.evaluation_id);
    await load();
  } catch (e) {
    error.value = String(e);
  }
}

onMounted(() => {
  if (adminAuth.value) void load();
});
watch(adminAuth, (t) => {
  if (t) void load();
});
</script>

<template>
  <h1 class="page-title">控制面</h1>
  <p class="page-sub">Evaluation、Job 与 Worker 的统一入口。判分仍由 core 控制面负责。</p>

  <div v-if="!authed" class="panel" style="max-width: 520px">
    <h2>管理员凭据</h2>
    <p class="hint">
      凭据只保存在当前浏览器 session，并通过同源代理发送到 control；不会写入任务数据。
    </p>
    <form class="loginrow" @submit.prevent="login">
      <el-input
        v-model="tokenInput"
        type="password"
        autocomplete="current-password"
        placeholder="GHOST_ADMIN_TOKEN"
        class="mono-input"
      />
      <el-button native-type="submit" type="primary">连接</el-button>
    </form>
  </div>

  <template v-else>
    <Banner v-if="error" tone="err" :text="`控制面请求失败：${error}`" />

    <div class="grid-metrics">
      <StatCard label="Evaluation 总数" :value="evaluations.length" />
      <StatCard label="排队中" :value="queued" tone="amber" />
      <StatCard label="运行中" :value="running" tone="green" />
      <StatCard label="在线 Worker" :value="onlineWorkers" />
    </div>

    <div class="panel">
      <div class="panel-head">
        <h2 style="margin: 0">创建 Evaluation</h2>
        <el-button size="small" text @click="resetSession">断开凭据</el-button>
      </div>
      <form class="createrow" @submit.prevent="void submitEvaluation()">
        <el-input
          v-model="taskToken"
          placeholder="task token"
          class="mono-input"
          style="min-width: 220px; flex: 1"
        />
        <el-input v-model="projectId" placeholder="project" class="mono-input" style="width: 128px" />
        <el-input
          v-model="idempotencyKey"
          placeholder="幂等 key（可选）"
          class="mono-input"
          style="width: 160px"
        />
        <el-button
          native-type="submit"
          type="primary"
          :disabled="saving || !taskToken.trim()"
        >
          {{ saving ? "创建中…" : "创建" }}
        </el-button>
      </form>
    </div>

    <div class="panel" style="padding: 0">
      <div class="panel-head" style="padding: 12px 16px 8px">
        <h2 style="margin: 0">Evaluations</h2>
        <el-button size="small" text @click="void load()">
          {{ loading ? "刷新中…" : "刷新" }}
        </el-button>
      </div>
      <el-table :data="evaluations" size="small" row-key="evaluation_id" empty-text="暂无 Evaluation">
        <el-table-column label="Evaluation" width="132">
          <template #default="{ row }">
            <span class="mono" style="font-size: 12px">{{ row.evaluation_id.slice(0, 12) }}…</span>
          </template>
        </el-table-column>
        <el-table-column label="Project" width="140">
          <template #default="{ row }">
            <span class="mono mut" style="font-size: 12px">{{ row.project_id }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="110" />
        <el-table-column label="Job 完成/总数" width="130">
          <template #default="{ row }">
            <span class="tnum mut">{{ row.completed_count }}/{{ row.job_count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="待/跑/败" width="110">
          <template #default="{ row }">
            <span class="tnum mut" style="font-size: 12px">
              {{ row.pending_count }}/{{ row.running_count }}/{{ row.failed_count }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">
            <span class="mut" style="font-size: 12px">{{ fmtDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="80">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'queued' || row.status === 'running'"
              size="small"
              text
              @click="void cancel(row as EvaluationRow)"
            >
              取消
            </el-button>
            <span v-else class="mut">—</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="panel" style="padding: 0">
      <div class="panel-head" style="padding: 12px 16px 8px">
        <h2 style="margin: 0">Workers</h2>
      </div>
      <el-table :data="workers" size="small" row-key="worker_id" empty-text="暂无 Worker">
        <el-table-column label="Worker" width="150">
          <template #default="{ row }">
            <span class="mono" style="font-size: 12px">{{ row.worker_id }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="110" />
        <el-table-column label="能力" min-width="200">
          <template #default="{ row }">
            <span class="mono mut" style="font-size: 11.5px">
              {{ Object.keys(row.capabilities).join(", ") || "—" }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="最近心跳" width="170">
          <template #default="{ row }">
            <span class="mut" style="font-size: 12px">{{ fmtDateTime(row.last_seen_at) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </template>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--dim);
}
.loginrow {
  display: flex;
  gap: 8px;
}
.createrow {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
</style>
