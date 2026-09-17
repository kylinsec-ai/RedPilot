<script setup lang="ts">
/**
 * 侧栏内容:品牌行 + 分组导航 + 底部实时态。
 * 只填充父级给定盒子;宽高与抽屉位移由 AppShell.vue 施加。
 */
import { computed } from "vue";
import LiveChip from "../components/LiveChip.vue";
import ChallengeList from "../components/ChallengeList.vue";
import { NAV, type NavEntry } from "../lib/nav";
import { current } from "../lib/route";
import { rosterData, toRows } from "../stores/roster";

withDefaults(defineProps<{ connText?: string }>(), { connText: "" });

const total = computed(() => toRows(rosterData.value).length);
const activeCode = computed(() =>
  current.value.view === "challenge" ? current.value.code : null,
);

function isActive(e: NavEntry): boolean {
  if (e.kind === "link") return e.isActive ? e.isActive(current.value.view) : false;
  return false;
}

/** 组头右侧的题目计数:只在花名册到达且本组含题目列表时给 */
function groupCount(entries: NavEntry[]): number | null {
  if (!rosterData.value || !entries.some((e) => e.kind === "challenges")) return null;
  return total.value;
}
</script>

<template>
  <div class="sidenav">
    <div class="brand">
      Ghost<small>平台控制台</small>
    </div>

    <nav class="nav scroll-thin">
      <template v-for="g in NAV" :key="g.key">
        <div v-if="g.label" class="group-head">
          <span class="group-label">{{ g.label }}</span>
          <span v-if="groupCount(g.entries) !== null" class="group-count tnum">
            {{ groupCount(g.entries) }}
          </span>
        </div>
        <template v-for="e in g.entries" :key="e.key">
          <ChallengeList v-if="e.kind === 'challenges'" :active-code="activeCode" />
          <a
            v-else
            :href="e.to"
            :aria-current="isActive(e) ? 'page' : undefined"
            class="navlink"
            :class="{ 'navlink-active': isActive(e) }"
          >
            {{ e.label }}
          </a>
        </template>
      </template>
    </nav>

    <div class="foot">
      <LiveChip />
      <div class="dim" style="font-size: 11px">{{ connText }}</div>
    </div>
  </div>
</template>

<style scoped>
.sidenav {
  display: flex;
  height: 100%;
  min-height: 0;
  flex-direction: column;
}
.brand {
  display: flex;
  height: 44px;
  flex: none;
  align-items: center;
  border-bottom: 1px solid var(--line);
  padding: 0 16px;
  font-weight: 700;
  letter-spacing: 0.4px;
}
.brand small {
  margin-left: 8px;
  font-weight: 400;
  color: var(--dim);
}
.nav {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 8px;
}
.group-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin: 12px 8px 4px;
}
.group-head:first-child {
  margin-top: 0;
}
.group-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--dim);
}
.group-count {
  font-size: 10.5px;
  color: var(--dim);
  opacity: 0.7;
}
.navlink {
  display: block;
  border-radius: 7px;
  padding: 6px 10px;
  font-size: 13px;
  color: var(--mut);
}
.navlink:hover {
  background: color-mix(in srgb, var(--panel2) 60%, transparent);
  color: var(--ink);
  text-decoration: none;
}
.navlink-active {
  background: var(--panel2);
  color: var(--ink);
}
.foot {
  flex: none;
  border-top: 1px solid var(--line);
  padding: 10px 16px;
}
.foot > * + * {
  margin-top: 4px;
}
</style>
