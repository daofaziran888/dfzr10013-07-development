<template>
  <section class="page" data-module="decision">
    <header class="page-head">
      <div>
        <h2>决策建议推送</h2>
        <p class="page-desc">
          从异常预警分级数据集读取预警，把影响映射为建议的收益与风险输入，并固定引用窗口版本。
          推送前核对版本与更正标记，避免沿用已更正却未标记的结论。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="loading" @click="generateBrief">按当前分级数据集生成建议</button>
        <button class="btn" type="button" :disabled="loading" @click="checkAll">推送前核对</button>
        <button class="btn" type="button" @click="loadAll">刷新</button>
      </div>
    </header>

    <p v-if="message" class="notice" :class="{ error: messageIsError }">{{ message }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">当前分级数据集版本</span>
        <strong class="stat-value">v{{ datasetVersion }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">建议条数</span>
        <strong class="stat-value">{{ briefs.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">核对不通过</span>
        <strong class="stat-value">{{ blockedCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">引用已更正预警</span>
        <strong class="stat-value">{{ correctedReferenceCount }}</strong>
      </article>
    </div>

    <h3>建议列表</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>建议编号</th><th>分析主题</th><th>收益评估</th><th>风险评估</th>
          <th>引用数据集</th><th>引用窗口</th><th>状态</th><th>核对</th><th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="brief in briefs" :key="brief.建议编号">
          <td class="mono">{{ brief.建议编号 }}</td>
          <td>{{ brief.分析主题 }}</td>
          <td>{{ brief.收益评估 }}</td>
          <td>{{ brief.风险评估 }}</td>
          <td>v{{ brief.引用数据集版本 }}</td>
          <td class="mono hint">
            <div v-for="reference in brief.引用窗口" :key="`${reference.窗口编号}-${reference.预警编号 ?? 'gap'}`">
              {{ reference.窗口编号 }} v{{ reference.窗口版本 }}
              <span v-if="reference.预警编号">· {{ reference.预警级别 }}</span>
              <span v-else>· 仅缺口</span>
            </div>
          </td>
          <td>
            {{ brief.状态 }}
            <span v-if="brief.被更正引用.length" class="badge badge-danger">含已更正引用</span>
          </td>
          <td>
            <span v-if="checkFor(brief.建议编号)" class="badge" :class="checkFor(brief.建议编号)?.可推送 ? 'badge-ok' : 'badge-danger'">
              {{ checkFor(brief.建议编号)?.可推送 ? '一致' : '需刷新' }}
            </span>
            <span v-else class="hint">未核对</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="selectBrief(brief.建议编号)">明细</button>
            <button class="link" type="button" @click="pushBrief(brief.建议编号)">核对推送</button>
          </td>
        </tr>
        <tr v-if="!briefs.length">
          <td colspan="9" class="empty-state">还没有按窗口版本生成的建议，可先点击“按当前分级数据集生成建议”</td>
        </tr>
      </tbody>
    </table>

    <section v-if="selectedBrief" class="panel">
      <div class="panel-head">
        <h3>{{ selectedBrief.建议编号 }} · {{ selectedBrief.分析主题 }}</h3>
        <span class="hint">引用数据集 v{{ selectedBrief.引用数据集版本 }} · 生成于 {{ selectedBrief.生成时间 }}</span>
      </div>

      <h4>收益输入</h4>
      <table class="data-table compact">
        <thead><tr><th>来源预警</th><th>窗口（版本）</th><th>预警级别</th><th>收益值</th><th>折算依据</th><th>来源观测</th></tr></thead>
        <tbody>
          <tr v-for="item in selectedBrief.收益输入" :key="item.来源预警 ?? item.窗口编号">
            <td class="mono">{{ item.来源预警 }}</td>
            <td class="mono">{{ item.窗口编号 }} v{{ item.窗口版本 }}</td>
            <td><span class="badge" :class="levelClass(item.预警级别)">{{ item.预警级别 }}</span></td>
            <td>{{ item.收益值 }}</td>
            <td class="hint">{{ item.依据 }}</td>
            <td class="mono">{{ item.来源观测.join('、') }}</td>
          </tr>
          <tr v-if="!selectedBrief.收益输入.length"><td colspan="6" class="empty-state">没有触发预警，收益输入为空</td></tr>
        </tbody>
      </table>

      <h4>风险输入</h4>
      <table class="data-table compact">
        <thead><tr><th>来源预警</th><th>窗口（版本）</th><th>风险类型</th><th>风险值</th><th>依据</th><th>来源观测</th></tr></thead>
        <tbody>
          <tr v-for="(item, index) in selectedBrief.风险输入" :key="index">
            <td class="mono">{{ item.来源预警 || '—' }}</td>
            <td class="mono">{{ item.窗口编号 }} v{{ item.窗口版本 }}</td>
            <td>{{ item.风险类型 }}</td>
            <td>{{ item.风险值 }}</td>
            <td class="hint">{{ item.依据 }}</td>
            <td class="mono">{{ item.来源观测.filter(Boolean).join('、') }}</td>
          </tr>
          <tr v-if="!selectedBrief.风险输入.length"><td colspan="6" class="empty-state">没有风险输入</td></tr>
        </tbody>
      </table>

      <h4>处置优先序</h4>
      <table class="data-table compact">
        <thead><tr><th>优先序</th><th>对象</th><th>优先级得分</th><th>处置时限</th><th>关联窗口</th></tr></thead>
        <tbody>
          <tr v-for="item in selectedBrief.处置优先序" :key="item.对象">
            <td>{{ item.优先序 }}</td>
            <td>{{ item.对象 }}</td>
            <td>{{ item.优先级得分 }}</td>
            <td>{{ item.处置时限 }}</td>
            <td class="mono">{{ Array.from(new Set(item.关联窗口)).join('、') }}</td>
          </tr>
        </tbody>
      </table>

      <h4>核对结果</h4>
      <table class="data-table compact">
        <thead><tr><th>类型</th><th>窗口</th><th>预警</th><th>说明</th><th>是否阻断</th></tr></thead>
        <tbody>
          <tr v-for="(issue, index) in checkFor(selectedBrief.建议编号)?.问题 ?? []" :key="index">
            <td>{{ issue.类型 }}</td>
            <td class="mono">{{ issue.窗口编号 || '—' }}</td>
            <td class="mono">{{ issue.预警编号 || '—' }}</td>
            <td class="hint">{{ issue.说明 }}</td>
            <td>{{ issue.仅提示 ? '仅提示' : '阻断' }}</td>
          </tr>
          <tr v-if="!(checkFor(selectedBrief.建议编号)?.问题 ?? []).length">
            <td colspan="5" class="empty-state">引用与当前分级数据集一致</td>
          </tr>
        </tbody>
      </table>

      <div v-if="selectedBrief.被更正引用.length" class="notice warn">
        <strong>已标记的已更正引用：</strong>
        <div v-for="item in selectedBrief.被更正引用" :key="item.预警编号">
          {{ item.预警编号 }}（原引用 {{ item.原引用级别 }}）· {{ item.说明 }}
        </div>
      </div>

      <div class="form-foot">
        <button class="btn" type="button" @click="markCorrected(selectedBrief.建议编号)">标记已更正引用</button>
        <button class="btn primary" type="button" @click="pushBrief(selectedBrief.建议编号)">核对并推送</button>
      </div>
    </section>

    <footer class="page-foot">
      <span>建议按生成时的窗口版本固定引用；分级数据集更新后需重新核对再推送。</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Reference = { 窗口编号: string; 窗口版本: number; 预警编号: string | null; 预警级别: string }
type InputItem = {
  来源预警: string | null
  窗口编号: string
  窗口版本: number
  预警级别?: string
  收益值?: number
  风险类型?: string
  风险值?: number
  依据: string
  来源观测: (string | null)[]
}
type Disposal = { 对象: string; 优先序: number; 优先级得分: number; 处置时限: string; 关联窗口: string[] }
type CorrectedRef = { 预警编号: string; 原引用级别: string; 说明: string }
type Brief = {
  建议编号: string
  分析主题: string
  收益评估: string
  风险评估: string
  状态: string
  生成时间: string
  引用数据集版本: number
  引用窗口: Reference[]
  收益输入: InputItem[]
  风险输入: InputItem[]
  处置优先序: Disposal[]
  被更正引用: CorrectedRef[]
}
type Issue = {
  类型: string
  窗口编号?: string
  预警编号?: string
  说明: string
  仅提示?: boolean
}
type CheckResult = { 建议编号: string; 可推送: boolean; 问题: Issue[] }

const ENDPOINT = '/api/decision'
const loading = ref(false)
const message = ref('')
const messageIsError = ref(false)
const errorMessage = ref('')
const datasetVersion = ref(0)
const briefs = ref<Brief[]>([])
const checks = ref<Record<string, CheckResult>>({})
const selectedBrief = ref<Brief | null>(null)

const blockedCount = computed(() => Object.values(checks.value).filter((item) => !item.可推送).length)
const correctedReferenceCount = computed(
  () => briefs.value.reduce((total, brief) => total + brief.被更正引用.length, 0),
)

function levelClass(level = '') {
  if (level === '严重') return 'badge-danger'
  if (level === '较重') return 'badge-warn'
  if (level === '一般') return 'badge-info'
  return 'badge-muted'
}

function checkFor(briefId: string) {
  return checks.value[briefId]
}

function notify(text: string, isError = false) {
  message.value = text
  messageIsError.value = isError
  errorMessage.value = isError ? text : ''
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail ?? `接口返回 ${response.status}`)
  }
  return payload as T
}

async function loadAll() {
  loading.value = true
  errorMessage.value = ''
  try {
    const [briefPayload, dataset] = await Promise.all([
      getJson<{ items: Brief[] }>(`${ENDPOINT}/briefs`),
      getJson<{ 数据集版本: number }>('/api/alert/dataset'),
    ])
    briefs.value = briefPayload.items
    datasetVersion.value = dataset.数据集版本
    if (selectedBrief.value) {
      selectedBrief.value = briefs.value.find((item) => item.建议编号 === selectedBrief.value?.建议编号) ?? null
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '决策建议读取失败'
  } finally {
    loading.value = false
  }
}

async function selectBrief(briefId: string) {
  try {
    selectedBrief.value = await getJson<Brief>(`${ENDPOINT}/briefs/${briefId}`)
    if (!checks.value[briefId]) {
      await checkOne(briefId)
    }
  } catch (error) {
    notify(error instanceof Error ? error.message : '建议明细读取失败', true)
  }
}

async function checkOne(briefId: string) {
  const payload = await getJson<{ 核对结果: CheckResult[] }>(`${ENDPOINT}/briefs/check?brief_id=${briefId}`)
  const result = payload.核对结果[0]
  if (result) checks.value = { ...checks.value, [result.建议编号]: result }
  return result
}

async function checkAll() {
  loading.value = true
  try {
    const payload = await getJson<{ ok: boolean; 核对结果: CheckResult[] }>(`${ENDPOINT}/briefs/check`)
    checks.value = Object.fromEntries(payload.核对结果.map((item) => [item.建议编号, item]))
    notify(payload.ok ? '全部建议引用与当前分级数据集一致' : '存在引用过期的建议，已标出需刷新项', !payload.ok)
  } catch (error) {
    notify(error instanceof Error ? error.message : '核对失败', true)
  } finally {
    loading.value = false
  }
}

async function generateBrief() {
  loading.value = true
  try {
    const result = await postJson<{ ok: boolean; message: string; entry?: Brief }>(`${ENDPOINT}/briefs`, { values: {} })
    notify(result.message || '建议已生成')
    if (result.entry) selectedBrief.value = result.entry
    await loadAll()
    if (result.entry) await checkOne(result.entry.建议编号)
  } catch (error) {
    notify(error instanceof Error ? error.message : '生成建议失败', true)
  } finally {
    loading.value = false
  }
}

async function pushBrief(briefId: string) {
  try {
    const result = await postJson<{ ok: boolean; message: string }>(
      `${ENDPOINT}/briefs/${briefId}/actions`,
      { values: { action: '核对推送' } },
    )
    notify(result.message, !result.ok)
    await checkOne(briefId)
    await loadAll()
  } catch (error) {
    notify(error instanceof Error ? error.message : '推送失败', true)
  }
}

async function markCorrected(briefId: string) {
  try {
    const result = await postJson<{ ok: boolean; message: string }>(
      `${ENDPOINT}/briefs/${briefId}/actions`,
      { values: { action: '标记已更正引用' } },
    )
    notify(result.message, !result.ok)
    await loadAll()
    await selectBrief(briefId)
  } catch (error) {
    notify(error instanceof Error ? error.message : '标记失败', true)
  }
}

onMounted(loadAll)
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin: 12px 0; }
.panel-head { display: flex; justify-content: space-between; align-items: center; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; }
.badge-danger { background: #fee4e2; color: #b42318; }
.badge-warn { background: #fef0c7; color: #b54708; }
.badge-info { background: #e0f2fe; color: #026aa2; }
.badge-muted { background: #f1f5f9; color: #475467; }
.badge-ok { background: #dcfae6; color: #067647; }
.notice { background: #eff6ff; border: 1px solid #bfdbfe; color: #1e3a8a; border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.notice.error { background: #fef3f2; border-color: #fecdca; color: #b42318; }
.notice.warn { background: #fffaeb; border-color: #fedf89; color: #b54708; }
.compact th, .compact td { font-size: 12px; }
.hint { color: var(--muted); font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.form-foot { display: flex; gap: 10px; margin-top: 10px; }
.data-table { margin-bottom: 8px; }
</style>
