<script setup lang="ts">
/**
 * 侧栏底部的实时态。判定只看 ACTIVE_PHASES(与旧版一致):
 * phase 为 error/done 时点会变色,文案仍是"待命轮询"——那是刻意的,
 * 因为"是否在解题"与"上一次结束得好不好"是两件事。
 */
import { computed } from "vue";
import { ACTIVE_PHASES } from "../types";
import { live } from "../stores/live";

const busy = computed(() => ACTIVE_PHASES.includes(live.value.phase));
const dotTone = computed(() => {
  if (live.value.phase === "error") return "dot-red";
  if (live.value.phase === "done") return "dot-green";
  if (busy.value) return "dot-amber pulse-ring";
  return "dot-dim";
});
</script>

<template>
  <div class="livechip">
    <span class="dot" :class="dotTone" />
    <span v-if="busy" class="mut">
      求解中：<b class="c-ink mono">{{ live.challenge_code }}</b> · {{ live.phase }}
      <template v-if="live.turns"> · {{ live.turns }} 轮</template>
    </span>
    <span v-else class="mut">待命轮询</span>
  </div>
</template>

<style scoped>
.livechip {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.dot {
  width: 9px;
  height: 9px;
  flex: none;
  border-radius: 999px;
}
</style>
