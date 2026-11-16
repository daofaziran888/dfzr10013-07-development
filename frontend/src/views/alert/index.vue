<template>
  <section class="page" data-module="alert">
    <header class="page-head">
      <div>
        <h2>异常预警中心</h2>
        <p class="page-desc">
          按事件时间把观测归入窗口，逐条使用观测当时生效的阈值版本，生成可逐项追溯的窗口预警分级数据集。
          缺报只记待补齐缺口，不按恢复处理；迟到更正保留原依据。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" :disabled="loading" @click="recompute">重算分级数据集</button>
        <button class="btn" type="button" @click="loadAll">刷新</button>
        <button class="btn primary" type="button" @click="showIngest = !showIngest">
          {{ showIngest ? '收起观测录入' : '登记观测' }}
        </button>
      </div>
    </header>

    <form v-if="showIngest" class="panel" @submit.prevent="submitObservation">
      <h3>登记观测（按接收顺序接入）</h3>
      <div class="form-grid">
        <label>观测编号<input v-model="observationForm['观测编号']" placeholder="OBS-SALE-101" /></label>
        <label>指标<input v-model="observationForm['指标']" placeholder="销售额" /></label>
        <label>维度<input v-model="observationForm['维度']" placeholder="区域=华东;渠道=线上" /></label>
        <label>事件时间<input v-model="observationForm['事件时间']" placeholder="2026-09-01T09:05:00" /></label>
        <label>接收时间<input v-model="observationForm['接收时间']" placeholder="2026-09-01T09:20:00" /></label>
        <label>数值<input v-model="observationForm['数值']" placeholder="135" /></label>
        <label class="wide">更正依据<input v-model="observationForm['更正依据']" placeholder="内容有变化时填写依据，留痕后不删除原依据" /></label>
      </div>
      <div class="form-foot">
        <button class="btn primary" type="submit" :disabled="loading">提交观测</button>
        <span class="hint">同一观测编号与内容重复提交只生效一次；内容不同按迟到更正处理。</span>
      </div>
    </form>

    <p v-if="message" class="notice" :class="{ error: messageIsError }">{{ message }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">分级数据集版本</span>
        <strong class="stat-value">v{{ datasetVersion }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">窗口数 / 预警数</span>
        <strong class="stat-value">{{ windows.length }} / {{ alerts.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已更正预警</span>
        <strong class="stat-value">{{ retired.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待补齐缺口</span>
        <strong class="stat-value">{{ gaps.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">重复观测忽略</span>
        <strong class="stat-value">{{ duplicateCount }}</strong>
      </article>
    </div>

    <nav class="tab-row">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- 窗口 -->
    <section v-if="activeTab === 'windows'">
      <div class="filter-bar">
        <label class="filter-item">
          <span>指标 / 维度 / 窗口编号</span>
          <input v-model="windowKeyword" placeholder="按关键字过滤" @keyup.enter="loadWindows" />
        </label>
        <label class="filter-item">
          <span>最高预警级别</span>
          <select v-model="windowLevel" @change="loadWindows">
            <option value="">全部</option>
            <option v-for="level in levelOptions" :key="level" :value="level">{{ level }}</option>
          </select>
        </label>
        <button class="btn" type="button" @click="loadWindows">查询</button>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>窗口</th><th>指标</th><th>维度</th><th>窗口区间</th><th>观测数</th>
            <th>最高级别</th><th>窗口版本</th><th>待补齐缺口</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="window in windows" :key="window.窗口编号">
            <td class="mono">{{ window.窗口编号 }}</td>
            <td>{{ window.指标 }}</td>
            <td>{{ window.维度 }}</td>
            <td class="mono">{{ window.窗口开始 }} ~ {{ window.窗口结束.slice(11) }}</td>
            <td>{{ window.观测数 }}</td>
            <td><span class="badge" :class="levelClass(window.预警级别)">{{ window.预警级别 || '无' }}</span></td>
            <td>v{{ window.窗口版本 }}</td>
            <td>
              {{ window.待补齐缺口.length }}
              <span v-if="window.阈值未覆盖" class="badge badge-warn">阈值未覆盖</span>
            </td>
            <td><button class="link" type="button" @click="selectWindow(window.窗口编号)">查看依据</button></td>
          </tr>
          <tr v-if="!windows.length"><td colspan="9" class="empty-state">当前没有窗口数据</td></tr>
        </tbody>
      </table>
    </section>

    <!-- 窗口明细 -->
    <section v-if="activeTab === 'detail' && selectedWindow" class="panel">
      <div class="panel-head">
        <h3>{{ selectedWindow.窗口编号 }} · {{ selectedWindow.指标 }} · {{ selectedWindow.维度 }}</h3>
        <span class="badge" :class="levelClass(selectedWindow.预警级别)">{{ selectedWindow.预警级别 || '无' }}</span>
      </div>
      <dl class="kv-grid">
        <div><dt>窗口区间</dt><dd class="mono">{{ selectedWindow.窗口开始 }} ~ {{ selectedWindow.窗口结束 }}</dd></div>
        <div><dt>窗口版本</dt><dd>v{{ selectedWindow.窗口版本 }}</dd></div>
        <div><dt>采集间隔</dt><dd>{{ selectedWindow.采集间隔分钟 }} 分钟</dd></div>
        <div><dt>观测数</dt><dd>{{ selectedWindow.观测数 }}</dd></div>
      </dl>

      <h4>窗口成员（按事件时间排序，共 {{ selectedWindow.窗口成员.length }} 条）</h4>
      <table class="data-table compact">
        <thead>
          <tr><th>观测编号</th><th>事件时间</th><th>接收时间</th><th>数值</th><th>版本</th><th>逐条实际阈值与判定</th></tr>
        </thead>
        <tbody>
          <tr v-for="member in selectedWindow.窗口成员" :key="member.观测编号">
            <td class="mono">{{ member.观测编号 }}</td>
            <td class="mono">{{ member.事件时间 }}</td>
            <td class="mono">{{ member.接收时间 }}</td>
            <td>{{ member.数值 }}</td>
            <td>v{{ member.观测版本 }}</td>
            <td>
              <div v-for="rule in member.适用规则" :key="rule.规则编号" class="judgement">
                <template v-if="rule.适用">
                  <code>{{ rule.规则编号 }} v{{ rule.规则版本 }}</code>
                  阈值 {{ rule.方向 === 'lt' ? '<' : '>' }} {{ rule.阈值上限 }} ·
                  <span :class="rule.是否超限 ? 'text-danger' : 'text-muted'">
                    {{ rule.是否超限 ? `超限 ${((rule.超出比例 ?? 0) * 100).toFixed(1)}%` : '未超限' }}
                  </span>
                </template>
                <template v-else>
                  <code>{{ rule.规则编号 }}</code>
                  <span class="text-muted">{{ rule.原因 }}</span>
                </template>
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <h4>实际阈值（按生效版本分组）</h4>
      <table class="data-table compact">
        <thead><tr><th>规则</th><th>版本</th><th>阈值上限</th><th>方向</th><th>该版本覆盖的观测</th></tr></thead>
        <tbody>
          <tr v-for="segment in selectedWindow.实际阈值" :key="segment.规则编号 + segment.规则版本">
            <td>{{ segment.规则编号 }}</td>
            <td>v{{ segment.规则版本 }}</td>
            <td>{{ segment.阈值上限 }}</td>
            <td>{{ segment.方向 === 'lt' ? '低于下限' : '超过上限' }}</td>
            <td class="mono">{{ segment.观测编号.join('、') }}</td>
          </tr>
        </tbody>
      </table>

      <h4>连续段与缺口</h4>
      <table class="data-table compact">
        <thead><tr><th>规则</th><th>连续超限</th><th>要求次数</th><th>状态</th><th>含缺口</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="(segment, index) in selectedWindow.连续段" :key="index">
            <td>{{ segment.规则编号 }}</td>
            <td>{{ segment.超限次数 }} / {{ requiredFor(segment.规则编号) }}</td>
            <td>{{ segment.达到连续要求 ? '已满足' : '未满足' }}</td>
            <td>{{ segment.状态 }}</td>
            <td>{{ segment.含缺口 ? '是' : '否' }}</td>
            <td class="hint">{{ segment.说明 }}</td>
          </tr>
          <tr v-if="!selectedWindow.连续段.length"><td colspan="6" class="empty-state">该窗口没有连续超限段</td></tr>
        </tbody>
      </table>
      <p v-if="selectedWindow.待补齐缺口.length" class="notice warn">
        待补齐缺口：{{ selectedWindow.待补齐缺口.map((gap: Gap) => gap.期望时间).join('、') }}。
        缺口不视为恢复，也不会自动清除预警。
      </p>
    </section>

    <!-- 预警 -->
    <section v-if="activeTab === 'alerts'">
      <table class="data-table">
        <thead>
          <tr>
            <th>预警</th><th>规则</th><th>指标 / 维度</th><th>窗口</th><th>级别</th>
            <th>实际阈值</th><th>连续次数</th><th>触发观测</th><th>状态</th><th>动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="alert in alerts" :key="alert.预警编号">
            <td class="mono">{{ alert.预警编号 }}</td>
            <td>{{ alert.规则编号 }}<br /><span class="hint">v{{ alert.规则版本.join('/') }}</span></td>
            <td>{{ alert.指标 }}<br /><span class="hint">{{ alert.维度 }}</span></td>
            <td class="mono">
              <button class="link" type="button" @click="selectWindow(alert.窗口编号)">{{ alert.窗口编号 }}</button>
              <br /><span class="hint">窗口 v{{ alert.窗口版本 }}</span>
            </td>
            <td><span class="badge" :class="levelClass(alert.预警级别)">{{ alert.预警级别 }}</span></td>
            <td class="mono">
              <span v-for="version in alert.阈值上限" :key="version">{{ version }} </span>
              <br /><span class="hint">{{ alert.实际阈值.map((item: Threshold) => `${item.规则版本}:${item.观测编号.length}条`).join(' / ') }}</span>
            </td>
            <td>{{ alert.实际连续次数 }} / {{ alert.连续超限次数 }}</td>
            <td class="mono">{{ alert.触发依据.超限观测编号.join('、') }}</td>
            <td>{{ alert.状态 }}</td>
            <td class="row-actions">
              <button
                v-for="action in alertActions"
                :key="action"
                class="link"
                type="button"
                @click="runAlertAction(action, alert)"
              >{{ action }}</button>
            </td>
          </tr>
          <tr v-if="!alerts.length"><td colspan="10" class="empty-state">当前没有触发的窗口预警</td></tr>
        </tbody>
      </table>

      <h4>已更正预警（保留原处置依据，不删除）</h4>
      <table class="data-table compact">
        <thead><tr><th>预警</th><th>窗口</th><th>规则</th><th>原级别</th><th>更正类型</th><th>更正依据</th></tr></thead>
        <tbody>
          <tr v-for="alert in retired" :key="alert.预警编号">
            <td class="mono">{{ alert.预警编号 }}</td>
            <td class="mono">{{ alert.窗口编号 }} v{{ alert.窗口版本 }}</td>
            <td>{{ alert.规则编号 }}</td>
            <td>{{ alert.原预警级别 }}</td>
            <td>{{ alert.更正类型 }}</td>
            <td class="hint">{{ alert.更正依据 }}</td>
          </tr>
          <tr v-if="!retired.length"><td colspan="6" class="empty-state">没有已更正预警</td></tr>
        </tbody>
      </table>
    </section>

    <!-- 观测 -->
    <section v-if="activeTab === 'observations'">
      <table class="data-table">
        <thead>
          <tr><th>观测编号</th><th>指标</th><th>维度</th><th>事件时间</th><th>接收时间</th><th>数值</th><th>状态</th><th>版本</th><th>重复</th><th>更正依据</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in observations" :key="row.id">
            <td class="mono">{{ row.观测编号 }}</td>
            <td>{{ row.指标 }}</td>
            <td>{{ row.维度 }}</td>
            <td class="mono">{{ row.事件时间 }}</td>
            <td class="mono">{{ row.接收时间 }}</td>
            <td>{{ row.数值 }}</td>
            <td>{{ row.状态 }}</td>
            <td>v{{ row.版本 }}</td>
            <td>{{ row.重复次数 }}</td>
            <td class="hint">{{ row.更正依据 || '—' }}</td>
          </tr>
          <tr v-if="!observations.length"><td colspan="10" class="empty-state">还没有观测</td></tr>
        </tbody>
      </table>
      <p class="hint">事件时间乱序时窗口仍按事件时间聚合；接收时间只用于登记顺序与更正审计。</p>
    </section>

    <!-- 规则 -->
    <section v-if="activeTab === 'rules'">
      <table class="data-table">
        <thead>
          <tr><th>规则</th><th>版本</th><th>指标</th><th>适用范围</th><th>生效条件</th><th>阈值</th><th>连续次数</th><th>生效区间</th><th>分级标准</th></tr>
        </thead>
        <tbody>
          <tr v-for="rule in rules" :key="rule.规则编号 + rule.规则版本">
            <td>{{ rule.规则编号 }}<br /><span class="hint">{{ rule.规则名称 }}</span></td>
            <td>v{{ rule.规则版本 }}</td>
            <td>{{ rule.指标 }}</td>
            <td>{{ rule.适用范围 }}</td>
            <td>{{ rule.生效条件 }}</td>
            <td>{{ rule.阈值上限 }}</td>
            <td>{{ rule.连续超限次数 }}</td>
            <td class="mono">{{ rule.生效区间.开始 }} ~ {{ rule.实际生效至 || '至今' }}</td>
            <td class="hint">
              <div v-for="level in rule.分级标准" :key="level.级别">{{ level.级别 }} ≥ {{ (level.最小超出比例 * 100).toFixed(0) }}%</div>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 更正审计 -->
    <section v-if="activeTab === 'corrections'">
      <table class="data-table">
        <thead>
          <tr><th>观测</th><th>版本变化</th><th>原内容</th><th>新内容</th><th>影响窗口</th><th>是否更正原预警</th><th>依据</th><th>变更摘要</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in corrections" :key="item.id">
            <td class="mono">{{ item.观测编号 }}</td>
            <td>v{{ item.原版本 }} → v{{ item.新版本 }}</td>
            <td class="mono hint">{{ item.原内容.事件时间 }} / {{ item.原内容.数值 }}</td>
            <td class="mono hint">{{ item.新内容.事件时间 }} / {{ item.新内容.数值 }}</td>
            <td class="mono">{{ item.影响窗口.join('、') }}</td>
            <td>{{ item.是否更正原预警 ? '是' : '否' }}</td>
            <td class="hint">{{ item.更正依据 }}</td>
            <td class="hint">
              <div v-for="(line, index) in item.变更摘要" :key="index">{{ line }}</div>
            </td>
          </tr>
          <tr v-if="!corrections.length"><td colspan="8" class="empty-state">没有迟到更正记录</td></tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>数据集版本 v{{ datasetVersion }} · 生成时间 {{ generatedAt || '—' }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Gap = { 期望时间: string; 前序观测编号: string; 后续观测编号: string | null }
type Threshold = { 规则编号: string; 规则版本: number; 阈值上限: number; 方向: string; 观测编号: string[] }
type RuleJudgement = { 规则编号: string; 规则版本?: number; 阈值上限?: number; 方向?: string; 适用: boolean; 是否超限?: boolean; 超出比例?: number; 原因?: string }
type Window = {
  窗口编号: string
  窗口版本: number
  指标: string
  维度: string
  窗口开始: string
  窗口结束: string
  采集间隔分钟: number
  观测数: number
  预警级别: string
  待补齐缺口: Gap[]
  阈值未覆盖: boolean
  窗口成员: { 观测编号: string; 事件时间: string; 接收时间: string; 数值: number; 观测版本: number; 适用规则: RuleJudgement[] }[]
  实际阈值: Threshold[]
  连续段: { 规则编号: string; 超限次数: number; 达到连续要求: boolean; 状态: string; 含缺口: boolean; 说明: string }[]
  规则判定: { 规则编号: string; 连续超限次数: number }[]
}
type Alert = {
  预警编号: string
  窗口编号: string
  窗口版本: number
  指标: string
  维度: string
  规则编号: string
  规则版本: number[]
  预警级别: string
  阈值上限: number[]
  实际阈值: { 规则编号: string; 规则版本: number; 阈值上限: number; 方向: string; 观测编号: string[] }[]
  连续超限次数: number
  实际连续次数: number
  状态: string
  触发依据: { 超限观测编号: string[] }
}
type Retired = {
  预警编号: string
  窗口编号: string
  窗口版本: number
  规则编号: string
  原预警级别: string
  更正类型: string
  更正依据: string
}
type Observation = {
  id: number
  观测编号: string
  指标: string
  维度: string
  事件时间: string
  接收时间: string
  数值: number
  状态: string
  版本: number
  重复次数: number
  更正依据: string
}
type Rule = {
  规则编号: string
  规则版本: number
  规则名称: string
  指标: string
  适用范围: string
  生效条件: string
  阈值上限: number
  连续超限次数: number
  生效区间: { 开始: string; 结束: string | null }
  实际生效至: string | null
  分级标准: { 级别: string; 最小超出比例: number }[]
}
type Correction = {
  id: number
  观测编号: string
  原版本: number
  新版本: number
  原内容: { 事件时间: string; 数值: number }
  新内容: { 事件时间: string; 数值: number }
  影响窗口: string[]
  是否更正原预警: boolean
  更正依据: string
  变更摘要: string[]
}

const ENDPOINT = '/api/alert'
const levelOptions = ['一般', '较重', '严重']
const alertActions = ['确认预警', '执行处理', '关闭预警']
const tabs = [
  { key: 'windows', label: '窗口分级' },
  { key: 'alerts', label: '窗口预警' },
  { key: 'observations', label: '观测明细' },
  { key: 'rules', label: '阈值规则' },
  { key: 'corrections', label: '更正审计' },
]
const emptyObservation = { 观测编号: '', 指标: '', 维度: '', 事件时间: '', 接收时间: '', 数值: '', 更正依据: '' }

const activeTab = ref('windows')
const showIngest = ref(false)
const loading = ref(false)
const message = ref('')
const messageIsError = ref(false)
const errorMessage = ref('')
const datasetVersion = ref(0)
const generatedAt = ref('')
const duplicateCount = ref(0)
const windows = ref<Window[]>([])
const alerts = ref<Alert[]>([])
const retired = ref<Retired[]>([])
const gaps = ref<Gap[]>([])
const observations = ref<Observation[]>([])
const rules = ref<Rule[]>([])
const corrections = ref<Correction[]>([])
const selectedWindow = ref<(Window & { 关联预警?: Alert[] }) | null>(null)
const windowKeyword = ref('')
const windowLevel = ref('')
const observationForm = ref<Record<string, string>>({ ...emptyObservation })


function levelClass(level: string) {
  if (level === '严重') return 'badge-danger'
  if (level === '较重') return 'badge-warn'
  if (level === '一般') return 'badge-info'
  return 'badge-muted'
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

async function loadDataset() {
  const dataset = await getJson<{
    数据集版本: number
    生成时间: string
    重复观测忽略数: number
    预警: Alert[]
    已更正预警: Retired[]
    待补齐缺口: Gap[]
  }>(`${ENDPOINT}/dataset`)
  datasetVersion.value = dataset.数据集版本
  generatedAt.value = dataset.生成时间 ?? ''
  duplicateCount.value = dataset.重复观测忽略数
  alerts.value = dataset.预警
  retired.value = dataset.已更正预警
  gaps.value = dataset.待补齐缺口
}

async function loadWindows() {
  const query = new URLSearchParams()
  if (windowKeyword.value) query.set('keyword', windowKeyword.value)
  if (windowLevel.value) query.set('level', windowLevel.value)
  const payload = await getJson<{ items: Window[] }>(`${ENDPOINT}/windows?${query.toString()}`)
  windows.value = payload.items
}

function requiredFor(ruleNo: string) {
  return selectedWindow.value?.规则判定.find((item) => item.规则编号 === ruleNo)?.连续超限次数 ?? '—'
}

async function selectWindow(windowId: string) {
  selectedWindow.value = await getJson(`${ENDPOINT}/windows/${windowId}`)
  activeTab.value = 'detail'
}

async function loadAll() {
  loading.value = true
  errorMessage.value = ''
  try {
    await Promise.all([loadDataset(), loadWindows()])
    const [observationPayload, rulePayload, correctionPayload] = await Promise.all([
      getJson<{ items: Observation[] }>(`${ENDPOINT}/observations`),
      getJson<{ items: Rule[] }>(`${ENDPOINT}/rules`),
      getJson<{ items: Correction[] }>(`${ENDPOINT}/corrections`),
    ])
    observations.value = observationPayload.items
    rules.value = rulePayload.items
    corrections.value = correctionPayload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预警数据读取失败'
  } finally {
    loading.value = false
  }
}

async function submitObservation() {
  loading.value = true
  try {
    const values = { ...observationForm.value, 数值: Number(observationForm.value.数值) }
    const result = await postJson<{ ok: boolean; message: string; entry?: { 结果?: string } }>(
      `${ENDPOINT}/observations`,
      { values },
    )
    notify(result.message || '观测已接入')
    observationForm.value = { ...emptyObservation }
    await loadAll()
  } catch (error) {
    notify(error instanceof Error ? error.message : '观测接入失败', true)
  } finally {
    loading.value = false
  }
}

async function runAlertAction(action: string, alert: Alert) {
  try {
    const result = await postJson<{ ok: boolean; message: string }>(
      `${ENDPOINT}/alerts/${alert.预警编号}/actions`,
      { values: { action } },
    )
    notify(result.message || '动作已执行', !result.ok)
    await loadAll()
  } catch (error) {
    notify(error instanceof Error ? error.message : '预警动作失败', true)
  }
}

async function recompute() {
  loading.value = true
  try {
    const result = await postJson<{ ok: boolean; message: string }>(`${ENDPOINT}/recompute`, { values: {} })
    notify(result.message || '重算完成')
    await loadAll()
  } catch (error) {
    notify(error instanceof Error ? error.message : '重算失败', true)
  } finally {
    loading.value = false
  }
}

onMounted(loadAll)
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; }
.form-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.form-grid .wide { grid-column: span 3; }
.form-grid input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.form-foot { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.tab-row { display: flex; gap: 6px; margin: 12px 0; }
.tab { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
.tab.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; }
.badge-danger { background: #fee4e2; color: #b42318; }
.badge-warn { background: #fef0c7; color: #b54708; }
.badge-info { background: #e0f2fe; color: #026aa2; }
.badge-muted { background: #f1f5f9; color: #475467; }
.notice { background: #eff6ff; border: 1px solid #bfdbfe; color: #1e3a8a; border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.notice.error { background: #fef3f2; border-color: #fecdca; color: #b42318; }
.notice.warn { background: #fffaeb; border-color: #fedf89; color: #b54708; }
.kv-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 8px 0; }
.kv-grid dt { font-size: 12px; color: var(--muted); }
.kv-grid dd { margin: 2px 0 0; font-size: 13px; }
.compact th, .compact td { font-size: 12px; }
.judgement { font-size: 12px; line-height: 1.6; }
.hint { color: var(--muted); font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.text-danger { color: #b42318; }
.text-muted { color: var(--muted); }
.filter-item select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.data-table { margin-bottom: 8px; }
</style>
