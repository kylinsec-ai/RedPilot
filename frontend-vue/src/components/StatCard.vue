<script setup lang="ts">
/** 统计卡:label 在下、数值在上,可选后缀(旧版 26px 大号等宽数字)。 */
withDefaults(
  defineProps<{
    label: string;
    value: string | number;
    suffix?: string;
    tone?: "plain" | "green" | "amber" | "red";
  }>(),
  { suffix: "", tone: "plain" },
);
</script>

<template>
  <el-card shadow="never" class="stat" body-class="stat-body">
    <div class="stat-value" :class="'c-' + (tone === 'plain' ? 'ink' : tone)">
      {{ value }}<small v-if="suffix" class="stat-suffix">{{ suffix }}</small>
    </div>
    <div class="stat-label">{{ label }}</div>
  </el-card>
</template>

<style scoped>
.stat {
  border-color: var(--line);
  background: var(--panel);
}
.stat :deep(.stat-body) {
  padding: 12px 16px;
}
.stat-value {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.15;
  font-variant-numeric: tabular-nums;
}
.stat-suffix {
  font-size: 13px;
  font-weight: 400;
  color: var(--mut);
}
.stat-label {
  font-size: 12px;
  color: var(--dim);
}
</style>
