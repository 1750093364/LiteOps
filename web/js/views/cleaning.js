/* 数据清洗视图（卡03 体检 + 卡04 清洗引擎）。
   文件列表 → 体检报告 / 清洗流程工作台 / 清洗记录。
   Vue3 全局构建，无构建步骤。整体包在 IIFE 内，避免与 app.js 在全局作用域重复 const 声明。 */
(function () {
const { ref, reactive, computed, onMounted } = Vue;

const ANOMALY_PAGE = 200;  // 异常清单前端渲染上限（后端全量存储，确认可批量）

// 动作类型定义：label / 是否破坏性（删行/删列需二次确认）/ 默认参数
const ACTION_DEFS = [
  { type: 'fill_missing', label: '缺失值处理', destructive: false,
    defaultParams: { field: '', strategy: 'fixed', value: '' } },
  { type: 'drop_duplicates', label: '去重', destructive: false,
    defaultParams: { columns: '' } },
  { type: 'convert_type', label: '类型转换', destructive: false,
    defaultParams: { field: '', target: 'number' } },
  { type: 'normalize_date', label: '日期标准化', destructive: false,
    defaultParams: { field: '', output_format: 'YYYY-MM-DD' } },
  { type: 'strip_text', label: '字符串修剪', destructive: false,
    defaultParams: { field: '' } },
  { type: 'filter_rows', label: '行筛选', destructive: true,
    defaultParams: { conditions: [{ field: '', operator: '=', value: '' }], combiner: 'and' } },
  { type: 'rename_field', label: '字段重命名', destructive: false,
    defaultParams: { old_name: '', new_name: '' } },
  { type: 'drop_field', label: '删除字段', destructive: true,
    defaultParams: { field: '' } },
  { type: 'sort_rows', label: '排序', destructive: false,
    defaultParams: { field: '', order: 'asc' } },
  { type: 'handle_anomaly', label: '异常值处理', destructive: true,
    defaultParams: { field: '', mode: 'drop', anomaly_rows: '' } },
];

const ACTION_LABEL = {};
const ACTION_DESTRUCTIVE = {};
ACTION_DEFS.forEach(d => {
  ACTION_LABEL[d.type] = d.label;
  ACTION_DESTRUCTIVE[d.type] = d.destructive;
});

// 可能删除整行的动作（fill_missing 的 drop_row 策略、filter_rows、handle_anomaly 的 drop 模式）
function actionDeletesRows(a) {
  if (a.type === 'fill_missing' && (a.params || {}).strategy === 'drop_row') return true;
  if (a.type === 'filter_rows') return true;
  if (a.type === 'handle_anomaly' && (a.params || {}).mode === 'drop') return true;
  return false;
}

window.CleaningView = {
  name: 'CleaningView',
  template: `
  <div class="cleaning-view">
    <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.msg }}</div>

    <!-- ================= 文件列表 ================= -->
    <template v-if="mode === 'list'">
      <div class="view-head">
        <div>
          <h1>数据清洗</h1>
          <p>对已导入文件做数据体检：缺失值 / 重复行 / 异常值 / 格式不符 / 类型误判</p>
        </div>
      </div>

      <table class="data-table">
        <thead>
          <tr><th>文件名</th><th>类型</th><th>行数</th><th>列数</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="loading"><td colspan="6" class="empty">加载中…</td></tr>
          <tr v-else-if="list.length === 0"><td colspan="6" class="empty">
            暂无文件，请先到「数据存储」导入数据
          </td></tr>
          <tr v-for="item in list" :key="item.id">
            <td class="strong">{{ item.origin_name }}</td>
            <td><span class="badge" :class="item.file_type">{{ typeText(item.file_type) }}</span></td>
            <td>{{ fmtNum(item.row_count) }}</td>
            <td>{{ fmtNum(item.col_count) }}</td>
            <td>
              <span class="badge" :class="item.status === '体检完成' ? 'ok' : 'status'">
                {{ item.status }}
              </span>
            </td>
            <td class="ops">
              <button class="btn btn-mini btn-primary" @click="openReport(item.id)">
                {{ item.status === '体检完成' ? '查看报告' : '开始体检' }}
              </button>
              <button class="btn btn-mini" @click="startClean(item)">开始清洗</button>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- ================= 报告 / 流程 / 记录 ================= -->
    <template v-else>
      <div class="view-head">
        <div class="flex-row">
          <button class="btn btn-mini" @click="backToList">← 返回列表</button>
          <h1 class="preview-title">{{ file.origin_name || '数据清洗' }}</h1>
          <span class="badge ok" v-if="report">体检完成</span>
        </div>
        <div class="flex-row" v-if="tab === 'profile'">
          <button class="btn" :disabled="running" @click="runProfile(null)">
            重新体检（全列）
          </button>
          <button class="btn btn-primary" @click="startClean(file)">开始清洗（选择预设）</button>
        </div>
        <div v-else></div>
      </div>

      <!-- 页内 tab -->
      <div class="profile-tabs">
        <button class="ptab" :class="{ active: tab === 'profile' }" @click="tab = 'profile'">体检报告</button>
        <button class="ptab" :class="{ active: tab === 'flow' }" @click="tab = 'flow'">
          清洗流程
        </button>
        <button class="ptab" :class="{ active: tab === 'records' }" @click="tab = 'records'; loadRecords()">
          清洗记录
        </button>
      </div>

      <!-- ============ 体检报告 ============ -->
      <template v-if="tab === 'profile'">
        <div v-if="loading" class="empty">加载中…</div>

        <div v-else-if="!report && !running" class="start-panel">
          <p class="start-desc">
            该文件共 <b>{{ fmtNum(file.row_count) }}</b> 行 × <b>{{ file.col_count }}</b> 列，尚未体检。
            点击「开始体检」后将自动扫描五类数据质量问题，报告生成后可随时回看。
          </p>
          <div class="dup-cols-pick">
            <div class="dcp-title">重复行检测范围（默认不勾 = 全列完全重复；勾选字段后增加按指定列检测）：</div>
            <label class="dcp-item" v-for="c in file.columns" :key="c">
              <input type="checkbox" :value="c" v-model="dupCols" />{{ c }}
            </label>
          </div>
          <button class="btn btn-primary" @click="runProfile(dupCols)">开始体检</button>
        </div>

        <div v-if="running" class="running-panel">
          <div class="running-icon">🔍</div>
          <div>正在扫描 {{ fmtNum(file.row_count) }} 行数据，请稍候…</div>
          <div class="text-sub">缺失 / 重复 / 异常 / 格式 / 类型 五项检测中</div>
        </div>

        <template v-if="report && !running">
          <div class="meta-bar">
            <span class="meta-item"><label>生成时间</label>{{ report.generated_at }}</span>
            <span class="meta-item"><label>耗时</label>{{ report.duration_sec }} 秒</span>
            <span class="meta-item"><label>扫描行数</label>{{ fmtNum(report.row_count) }}</span>
            <span class="meta-item"><label>重复检测</label>{{ report.dup_columns_used ? '按指定列 ' + report.dup_columns_used.join('、') : '全列完全重复' }}</span>
          </div>

          <div class="stat-grid">
            <div class="stat-card">
              <div class="stat-num" :class="{ danger: s.missing_warn_cnt > 0 }">{{ s.missing_fields_cnt }}</div>
              <div class="stat-label">缺失字段</div>
              <div class="stat-sub" :class="{ 'sub-danger': s.missing_warn_cnt > 0 }">
                {{ s.missing_warn_cnt > 0 ? s.missing_warn_cnt + ' 个字段缺失率 &gt;20%' : '无缺失预警字段' }}
              </div>
            </div>
            <div class="stat-card">
              <div class="stat-num" :class="{ danger: s.dup_rows > 0 }">{{ s.dup_rows }}</div>
              <div class="stat-label">重复行</div>
              <div class="stat-sub">{{ s.dup_groups }} 组重复</div>
            </div>
            <div class="stat-card">
              <div class="stat-num" :class="{ danger: s.anomaly_cnt > 0, warn: s.anomaly_truncated }">{{ s.anomaly_cnt }}</div>
              <div class="stat-label">异常值</div>
              <div class="stat-sub">{{ s.anomaly_truncated ? '清单已截断（仅记录前 5000 条）' : '规则阈值 + 3σ 双策略' }}</div>
            </div>
            <div class="stat-card">
              <div class="stat-num" :class="{ danger: s.format_issue_cnt > 0 }">{{ s.format_issue_cnt }}</div>
              <div class="stat-label">格式问题</div>
              <div class="stat-sub">另有 {{ s.type_suggest_cnt }} 个类型转换建议</div>
            </div>
          </div>

          <!-- ① 缺失值 -->
          <div class="profile-section">
            <h2 class="section-title">① 缺失值分析（空值 / 空字符串 / NA、N/A、null、-- 统一识别）</h2>
            <div class="miss-field-list">
              <div class="miss-field-row" v-for="f in report.missing.fields" :key="f.field">
                <span class="miss-field-name" :title="f.field">{{ f.field }}</span>
                <div class="miss-bar"><div class="miss-fill"
                     :class="{ danger: f.warn }"
                     :style="{ width: Math.max(f.rate * 100, f.missing > 0 ? 1.5 : 0) + '%' }"></div></div>
                <span class="miss-num" :class="{ danger: f.warn }">
                  {{ (f.rate * 100).toFixed(2) }}%
                </span>
                <span class="miss-detail">{{ fmtNum(f.missing) }} / {{ fmtNum(f.total) }} 行缺失
                  <em v-if="f.warn" class="warn-tag">缺失率 &gt;20%，预警</em>
                </span>
              </div>
            </div>
          </div>

          <!-- ② 重复行 -->
          <div class="profile-section">
            <h2 class="section-title">② 重复行检测</h2>
            <div class="dup-toolbar">
              <button class="btn btn-mini" :class="{ 'btn-primary': dupView === 'full' }" @click="dupView = 'full'">
                全列完全重复（{{ report.duplicates.full.dup_rows }} 行 / {{ report.duplicates.full.dup_groups }} 组）
              </button>
              <button class="btn btn-mini" :class="{ 'btn-primary': dupView === 'sub' }"
                      v-if="report.duplicates.by_columns" @click="dupView = 'sub'">
                按指定列 {{ report.duplicates.by_columns.columns.join('、') }}
                （{{ report.duplicates.by_columns.dup_rows }} 行 / {{ report.duplicates.by_columns.dup_groups }} 组）
              </button>
              <button class="btn btn-mini" @click="showDup = !showDup">
                {{ showDup ? '收起明细' : '展开重复明细（前 20 组）' }}
              </button>
            </div>
            <div class="dup-recheck">
              <label class="text-sub">按指定列重新检测：</label>
              <label class="dcp-item" v-for="c in report.columns" :key="'rc' + c">
                <input type="checkbox" :value="c" v-model="dupCols" />{{ c }}
              </label>
              <button class="btn btn-mini" :disabled="running || dupCols.length === 0" @click="runProfile(dupCols)">重新体检</button>
            </div>
            <table class="data-table dup-table" v-if="showDup">
              <thead>
                <tr><th style="width:50px">#</th><th style="width:220px">重复行号</th>
                  <th style="width:90px">出现次数</th><th>行内容</th></tr>
              </thead>
              <tbody>
                <tr v-if="curDup.samples.length === 0">
                  <td colspan="4" class="empty ok-text">未发现重复行</td>
                </tr>
                <tr v-for="(g, i) in curDup.samples" :key="i">
                  <td class="row-idx">{{ i + 1 }}</td>
                  <td>{{ g.row_nos.join('、') }}<span v-if="g.extra > 0" class="text-sub"> 等 {{ g.row_nos.length + g.extra }} 行</span></td>
                  <td>{{ g.row_nos.length + g.extra }} 次</td>
                  <td class="dup-values">
                    <span class="kv-chip" v-for="(v, k) in g.values" :key="k">
                      <b>{{ k }}</b>=<span :class="{ 'cell-null': v === '' }">{{ v === '' ? '（空）' : v }}</span>
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- ③ 异常值 -->
          <div class="profile-section">
            <h2 class="section-title">③ 异常值清单（规则：年龄 0~120、金额非负；统计：均值 ± 3σ）</h2>
            <div class="anomaly-toolbar">
              <label><input type="checkbox" :checked="allVisibleChecked" @change="toggleAllVisible($event)" /> 全选本页</label>
              <button class="btn btn-mini btn-primary" :disabled="selectedIds.length === 0" @click="confirmSelected">
                批量确认已选（{{ selectedIds.length }}）
              </button>
              <button class="btn btn-mini" @click="confirmAll">确认全部（{{ report.anomalies.total }} 条）</button>
              <span class="text-sub">已确认 {{ confirmedCnt }} 条 ·
                清单仅展示前 {{ ANOMALY_PAGE }} 条，共 {{ report.anomalies.total }} 条</span>
            </div>
            <div class="table-wrap">
              <table class="data-table anomaly-table">
                <thead>
                  <tr><th style="width:40px"></th><th style="width:70px">行号</th>
                    <th style="width:110px">字段</th><th style="width:130px">值</th><th>命中规则</th><th style="width:80px">状态</th></tr>
                </thead>
                <tbody>
                  <tr v-if="report.anomalies.items.length === 0">
                    <td colspan="6" class="empty ok-text">未发现异常值</td>
                  </tr>
                  <tr v-for="a in visibleAnomalies" :key="a.id" :class="{ confirmed: a.confirmed }">
                    <td><input type="checkbox" :disabled="a.confirmed"
                           :checked="a.confirmed || selectedIds.includes(a.id)" @change="toggleOne(a)" /></td>
                    <td class="row-idx">{{ a.row_no }}</td>
                    <td><span class="badge hit">{{ a.field }}</span></td>
                    <td class="anomaly-value">{{ a.value }}</td>
                    <td class="anomaly-rules">{{ a.rules }}</td>
                    <td>
                      <span class="badge" :class="a.confirmed ? 'ok' : 'status'">
                        {{ a.confirmed ? '已确认' : '待确认' }}
                      </span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- ④ 格式不符 -->
          <div class="profile-section">
            <h2 class="section-title">④ 格式不符项</h2>
            <div v-if="report.formats.date_mixed.length === 0 && report.formats.phones.length === 0
                        && report.formats.idcards.length === 0 && report.formats.spaces.length === 0" class="empty ok-text">
              未发现格式问题
            </div>
            <div class="issue-card" v-for="d in report.formats.date_mixed" :key="'dm' + d.field">
              <div class="issue-head"><span class="badge hit">日期混用</span><b>字段「{{ d.field }}」混用 {{ d.formats.length }} 种日期格式</b></div>
              <div class="fmt-chips">
                <span class="kv-chip" v-for="f in d.formats" :key="f.format">
                  {{ f.format }}：<b>{{ fmtNum(f.count) }}</b> 条
                </span>
              </div>
              <div class="issue-samples">
                <span class="text-sub">样例：</span>
                <span class="kv-chip" v-for="(vals, fmt) in d.samples" :key="fmt">
                  {{ fmt.split('（')[0] }} → {{ vals.join('、') }}
                </span>
              </div>
            </div>
            <div class="issue-card" v-for="p in report.formats.phones" :key="'ph' + p.field">
              <div class="issue-head"><span class="badge hit">手机号</span><b>字段「{{ p.field }}」有 {{ p.bad_count }} 个值不是 11 位数字</b></div>
              <div class="issue-items">
                <span class="kv-chip bad" v-for="(it, i) in p.items" :key="i">
                  第 {{ it.row_no }} 行：{{ it.value }}（{{ it.value.length }} 位）
                </span>
              </div>
            </div>
            <div class="issue-card" v-for="p in report.formats.idcards" :key="'id' + p.field">
              <div class="issue-head"><span class="badge hit">身份证</span><b>字段「{{ p.field }}」有 {{ p.bad_count }} 个值不是 18 位</b></div>
              <div class="issue-items">
                <span class="kv-chip bad" v-for="(it, i) in p.items" :key="i">
                  第 {{ it.row_no }} 行：{{ it.value }}
                </span>
              </div>
            </div>
            <div class="issue-card" v-for="sp in report.formats.spaces" :key="'sp' + sp.field">
              <div class="issue-head"><span class="badge hit">首尾空格</span><b>字段「{{ sp.field }}」有 {{ sp.bad_count }} 个值含首尾空格</b>
                <span class="text-sub">（展示前 {{ sp.items.length }} 条，␠ 表示空格）</span>
              </div>
              <div class="issue-items">
                <span class="kv-chip bad" v-for="(it, i) in sp.items" :key="i">
                  第 {{ it.row_no }} 行：<span class="space-val">「{{ showSpace(it.value) }}」</span>
                </span>
              </div>
            </div>
          </div>

          <!-- ⑤ 类型误判建议 -->
          <div class="profile-section">
            <h2 class="section-title">⑤ 类型误判与转换建议</h2>
            <div v-if="report.type_suggestions.length === 0" class="empty ok-text">未发现类型误判</div>
            <div class="suggest-grid">
              <div class="suggest-card" v-for="sg in report.type_suggestions" :key="sg.id">
                <div class="suggest-head">
                  <span class="badge" :class="sg.kind === 'number' ? 'csv' : 'cleaned'">
                    {{ sg.kind === 'number' ? '建议转数值' : '建议转日期' }}
                  </span>
                  <b>字段「{{ sg.field }}」</b>
                </div>
                <div class="suggest-reason">{{ sg.reason }}</div>
                <div class="suggest-sample">样例值：<code>{{ sg.sample }}</code></div>
                <button class="btn btn-mini btn-primary" @click="joinFlow(sg)">加入清洗流程 →</button>
              </div>
            </div>
          </div>
        </template>
      </template>

      <!-- ============ 清洗流程工作台 ============ -->
      <template v-if="tab === 'flow'">
        <div class="flow-workbench">
          <!-- 左：动作面板 -->
          <div class="flow-palette">
            <h3 class="palette-title">可添加的清洗动作</h3>
            <div class="palette-hint">点击动作添加到右侧流程</div>
            <div class="palette-list">
              <div class="palette-item" v-for="d in actionDefs" :key="d.type"
                   :class="{ destructive: d.destructive }" @click="addAction(d.type)">
                <span class="pi-label">{{ d.label }}</span>
                <span class="pi-type">{{ d.type }}</span>
                <span class="pi-add">+</span>
              </div>
            </div>
          </div>

          <!-- 右：流程编排 -->
          <div class="flow-area">
            <div class="flow-toolbar">
              <span class="flow-count">已编排 {{ flow.length }} 个动作</span>
              <span class="text-sub" v-if="unconfirmedCount > 0">
                有 {{ unconfirmedCount }} 个破坏性动作未勾选确认
              </span>
            </div>

            <div class="flow-list" v-if="flow.length > 0">
              <div class="flow-card" v-for="(a, i) in flow" :key="a.id"
                   :class="{ destructive: isDestructive(a), 'drag-over': dragIndex === i }"
                   draggable="true"
                   @dragstart="onDragStart(i)"
                   @dragover.prevent="onDragOver(i)"
                   @drop.prevent="onDrop(i)"
                   @dragend="onDragEnd">
                <div class="fc-head">
                  <span class="fc-drag">⋮⋮</span>
                  <span class="fc-idx">{{ i + 1 }}</span>
                  <span class="fc-name">{{ actionLabel(a.type) }}</span>
                  <span class="badge" :class="isDestructive(a) ? 'warn' : 'status'">
                    {{ isDestructive(a) ? '破坏性' : '安全' }}
                  </span>
                  <div class="fc-ops">
                    <button class="btn btn-mini" @click="moveAction(i, -1)" :disabled="i === 0">↑</button>
                    <button class="btn btn-mini" @click="moveAction(i, 1)" :disabled="i === flow.length - 1">↓</button>
                    <button class="btn btn-mini btn-danger" @click="removeAction(i)">删除</button>
                  </div>
                </div>

                <!-- 参数区 -->
                <div class="fc-params">
                  <template v-if="a.type === 'fill_missing'">
                    <label>字段
                      <select v-model="a.params.field"><option value="">请选择</option>
                        <option v-for="c in columns" :key="c" :value="c">{{ c }}</option></select>
                    </label>
                    <label>策略
                      <select v-model="a.params.strategy">
                        <option value="fixed">固定值</option>
                        <option value="mean">均值</option>
                        <option value="median">中位数</option>
                        <option value="ffill">前向填充</option>
                        <option value="bfill">后向填充</option>
                        <option value="drop_row">删除缺失行</option>
                      </select>
                    </label>
                    <label v-if="a.params.strategy === 'fixed'">填充值
                      <input type="text" v-model="a.params.value" placeholder="如 未知" />
                    </label>
                  </template>

                  <template v-else-if="a.type === 'drop_duplicates'">
                    <label>去重列（逗号分隔，留空 = 全列）
                      <input type="text" v-model="a.params.columns" :placeholder="columns.join(', ')" />
                    </label>
                  </template>

                  <template v-else-if="a.type === 'convert_type'">
                    <label>字段
                      <select v-model="a.params.field"><option value="">请选择</option>
                        <option v-for="c in columns" :key="c" :value="c">{{ c }}</option></select>
                    </label>
                    <label>目标类型
                      <select v-model="a.params.target">
                        <option value="number">数值（去千分位逗号）</option>
                        <option value="date">日期</option>
                      </select>
                    </label>
                  </template>

                  <template v-else-if="a.type === 'normalize_date'">
                    <label>字段
                      <select v-model="a.params.field"><option value="">请选择</option>
                        <option v-for="c in columns" :key="c" :value="c">{{ c }}</option></select>
                    </label>
                    <label>输出格式
                      <input type="text" v-model="a.params.output_format" />
                    </label>
                  </template>

                  <template v-else-if="a.type === 'strip_text'">
                    <label>字段
                      <select v-model="a.params.field"><option value="">请选择</option>
                        <option v-for="c in columns" :key="c" :value="c">{{ c }}</option></select>
                    </label>
                  </template>

                  <template v-else-if="a.type === 'filter_rows'">
                    <label>组合方式
                      <select v-model="a.params.combiner">
                        <option value="and">且（AND）</option>
                        <option value="or">或（OR）</option>
                      </select>
                    </label>
                    <div class="filter-conds">
                      <div class="filter-cond" v-for="(c, ci) in a.params.conditions" :key="ci">
                        <select v-model="c.field"><option value="">字段</option>
                          <option v-for="col in columns" :key="col" :value="col">{{ col }}</option></select>
                        <select v-model="c.operator">
                          <option value="=">=</option><option value="in">in</option>
                          <option value=">">></option><option value=">=">>=</option>
                          <option value="<"><</option><option value="<="><=</option>
                          <option value="contains">包含</option>
                        </select>
                        <input type="text" v-model="c.value" placeholder="值" />
                        <button class="btn btn-mini btn-danger" @click="removeFilterCond(a, ci)"
                                v-if="a.params.conditions.length > 1">×</button>
                      </div>
                      <button class="btn btn-mini" @click="addFilterCond(a)">+ 添加条件</button>
                    </div>
                  </template>

                  <template v-else-if="a.type === 'rename_field'">
                    <label>原字段
                      <select v-model="a.params.old_name"><option value="">请选择</option>
                        <option v-for="c in columns" :key="c" :value="c">{{ c }}</option></select>
                    </label>
                    <label>新字段名
                      <input type="text" v-model="a.params.new_name" placeholder="新名称" />
                    </label>
                  </template>

                  <template v-else-if="a.type === 'drop_field'">
                    <label>字段
                      <select v-model="a.params.field"><option value="">请选择</option>
                        <option v-for="c in columns" :key="c" :value="c">{{ c }}</option></select>
                    </label>
                  </template>

                  <template v-else-if="a.type === 'sort_rows'">
                    <label>字段
                      <select v-model="a.params.field"><option value="">请选择</option>
                        <option v-for="c in columns" :key="c" :value="c">{{ c }}</option></select>
                    </label>
                    <label>顺序
                      <select v-model="a.params.order">
                        <option value="asc">升序</option>
                        <option value="desc">降序</option>
                      </select>
                    </label>
                  </template>

                  <template v-else-if="a.type === 'handle_anomaly'">
                    <label>字段
                      <select v-model="a.params.field"><option value="">（可选）</option>
                        <option v-for="c in columns" :key="c" :value="c">{{ c }}</option></select>
                    </label>
                    <label>处理方式
                      <select v-model="a.params.mode">
                        <option value="drop">剔除异常行</option>
                        <option value="mark">标记（新增 is_anomaly 列）</option>
                      </select>
                    </label>
                    <label>异常行号（逗号分隔，留空取体检报告全部异常行）
                      <input type="text" v-model="a.params.anomaly_rows" :placeholder="anomalyRowHint" />
                    </label>
                  </template>
                </div>

                <!-- 预览按钮 -->
                <div class="fc-preview-bar">
                  <button class="btn btn-mini" :disabled="previewing" @click="previewAction(a)">
                    在样本上预览
                  </button>
                  <span class="text-sub" v-if="a.preview && a.preview.ok">
                    影响 {{ a.preview.affected_rows }} 行（样本 {{ a.preview.rows_before }} → {{ a.preview.rows_after }}）
                  </span>
                  <span class="text-sub error" v-else-if="a.preview && !a.preview.ok">
                    预览失败：{{ a.preview.error }}
                  </span>
                </div>
                <div class="fc-preview-detail" v-if="a.preview && a.preview.ok && a.preview.samples.length">
                  <div class="sample-pair" v-for="(p, pi) in a.preview.samples" :key="pi">
                    <div class="sp-before"><b>前：</b><span v-for="(v, k) in p.before" :key="k" class="kv-chip">{{ k }}={{ v }}</span></div>
                    <div class="sp-after"><b>后：</b><span v-for="(v, k) in p.after" :key="k" class="kv-chip">{{ k }}={{ v }}</span></div>
                  </div>
                </div>

                <!-- 破坏性动作确认 -->
                <div class="fc-confirm" v-if="isDestructive(a)">
                  <label class="confirm-check">
                    <input type="checkbox" v-model="a.confirmed" />
                    我确认此操作可能删除数据（{{ confirmDesc(a) }}）
                  </label>
                </div>
              </div>
            </div>

            <div class="flow-empty" v-else>
              <div class="flow-icon">🧹</div>
              <p>从左侧点击动作，开始编排清洗流程</p>
              <p class="text-sub" v-if="pendingSuggestions.length">
                已带入 {{ pendingSuggestions.length }} 个体检建议：{{ pendingSuggestionsText }}
              </p>
            </div>

            <!-- 底部操作栏 -->
            <div class="flow-footer">
              <span class="text-sub flow-preset-tag" v-if="currentPreset">
                来源预设：{{ currentPreset.name }}
              </span>
              <button class="btn" @click="startClean(file)">套用预设</button>
              <button class="btn" :disabled="flow.length === 0" @click="openSavePreset">
                保存为预设
              </button>
              <button class="btn btn-primary" :disabled="!canExecute || executing" @click="executeFlow">
                {{ executing ? '执行中…' : '执行清洗' }}
              </button>
            </div>
          </div>
        </div>
      </template>

      <!-- ============ 清洗记录（时间线） ============ -->
      <template v-if="tab === 'records'">
        <div v-if="recordsLoading" class="empty">加载中…</div>
        <div v-else-if="records.length === 0" class="empty">暂无清洗记录</div>
        <div class="record-timeline" v-else>
          <div class="rt-item" v-for="r in records" :key="r.id">
            <div class="rt-dot"></div>
            <div class="record-card">
              <div class="rc-head">
                <span class="rc-time">{{ r.created_at }}</span>
                <span class="badge" :class="r.preset_builtin ? 'hit' : 'status'" v-if="r.preset_name">
                  {{ r.preset_builtin ? '内置预设' : '自建预设' }}
                </span>
                <span class="badge cleaned" v-else>自定义流程</span>
                <span class="rc-preset">{{ r.preset_name || '' }}</span>
                <span class="rc-name">{{ r.output_name }}</span>
                <button class="btn btn-mini btn-danger" @click="rollbackRecord(r)">回滚</button>
              </div>
              <div class="rc-stats">
                <span>行数：{{ fmtNum(r.stats.rows_before) }} → {{ fmtNum(r.stats.rows_after) }}（删除 {{ fmtNum(r.stats.rows_removed) }} 行）</span>
                <span>列数：{{ r.stats.cols_before }} → {{ r.stats.cols_after }}</span>
                <span>耗时：{{ r.stats.duration_sec ? r.stats.duration_sec + 's' : '—' }}</span>
              </div>
              <div class="rc-summary">
                <span class="kv-chip sum" v-for="(t, i) in recordSummary(r.stats)" :key="i">{{ t }}</span>
              </div>
              <div class="rc-foot">
                <div class="rc-actions">
                  <span class="kv-chip" v-for="(a, i) in r.actions" :key="i">{{ actionLabel(a.type) }}</span>
                </div>
                <a class="rc-output-link" href="javascript:void(0)"
                   v-if="r.output_file_id" @click="gotoOutput()">产物：{{ r.output_name }}（{{ fmtNum(r.output_rows) }} 行）→ 去数据存储查看</a>
              </div>
            </div>
          </div>
        </div>
      </template>
    </template>

    <!-- 执行结果弹窗 -->
    <div class="modal-mask" v-if="resultModal.show" @click.self="resultModal.show = false">
      <div class="modal result-modal">
        <h3>清洗完成</h3>
        <div class="modal-body">
          <p>产物：<b>{{ resultModal.output_name }}</b></p>
          <div class="result-stats">
            <div class="rs-item" v-if="resultModal.stats.missing_filled > 0">
              <b>{{ fmtNum(resultModal.stats.missing_filled) }}</b><span>处理缺失（处）</span>
            </div>
            <div class="rs-item" v-if="resultModal.stats.rows_dropped_dedup > 0">
              <b>{{ fmtNum(resultModal.stats.rows_dropped_dedup) }}</b><span>去重（行）</span>
            </div>
            <div class="rs-item" v-if="resultModal.stats.fields_converted_cnt > 0">
              <b>{{ resultModal.stats.fields_converted_cnt }}</b><span>类型转换（字段）</span>
            </div>
            <div class="rs-item" v-if="resultModal.stats.rows_dropped_anomaly > 0">
              <b>{{ fmtNum(resultModal.stats.rows_dropped_anomaly) }}</b><span>剔除异常（行）</span>
            </div>
            <div class="rs-item" v-if="resultModal.stats.strip_count > 0">
              <b>{{ fmtNum(resultModal.stats.strip_count) }}</b><span>修剪空格（处）</span>
            </div>
          </div>
          <p>行数：{{ fmtNum(resultModal.stats.rows_before) }} → {{ fmtNum(resultModal.stats.rows_after) }}
            （删除 {{ fmtNum(resultModal.stats.rows_removed) }} 行）</p>
          <p>列数：{{ resultModal.stats.cols_before }} → {{ resultModal.stats.cols_after }}</p>
          <p>耗时：{{ resultModal.stats.duration_sec }} 秒</p>
          <p class="text-sub">产物已写入「数据存储 → 清洗产物」，统计已随清洗记录永久保存，可在「清洗记录」回看</p>
        </div>
        <div class="modal-foot">
          <button class="btn btn-primary" @click="resultModal.show = false">确定</button>
        </div>
      </div>
    </div>

    <!-- 预设选择弹窗 -->
    <div class="modal-mask" v-if="presetModal.show" @click.self="presetModal.show = false">
      <div class="modal preset-modal">
        <h3>选择清洗预设</h3>
        <p class="text-sub">为「{{ presetModal.file_name }}」选择一个预设流程，套用后可再微调动作与参数</p>
        <div class="preset-list" v-if="!presetModal.loading">
          <div class="preset-item blank" @click="chooseBlankFlow">
            <div class="pi-head"><span class="badge status">空白</span><b>空白流程</b></div>
            <div class="pi-desc">不套用预设，从零开始编排清洗动作</div>
          </div>
          <div class="preset-item" v-for="p in presets" :key="p.id" @click="choosePreset(p)">
            <div class="pi-head">
              <span class="badge" :class="p.is_builtin ? 'hit' : 'cleaned'">{{ p.is_builtin ? '内置' : '自建' }}</span>
              <b>{{ p.name }}</b>
              <span class="pi-cnt">{{ p.actions_cnt }} 个动作</span>
              <button class="btn btn-mini btn-danger" v-if="!p.is_builtin"
                      @click.stop="deletePreset(p)">删除</button>
            </div>
            <div class="pi-desc">{{ p.scenario || '（无场景描述）' }}</div>
          </div>
        </div>
        <div v-else class="empty">加载中…</div>
        <div class="modal-foot">
          <button class="btn" @click="presetModal.show = false">取消</button>
        </div>
      </div>
    </div>

    <!-- 保存为预设弹窗 -->
    <div class="modal-mask" v-if="saveModal.show" @click.self="saveModal.show = false">
      <div class="modal">
        <h3>保存为清洗预设</h3>
        <div class="modal-body">
          <label class="fm-label">预设名称
            <input type="text" v-model="saveModal.name" maxlength="50" placeholder="如：活动订单数据标准清洗" />
          </label>
          <label class="fm-label">适用场景描述
            <textarea v-model="saveModal.scenario" rows="3"
                      placeholder="描述该预设适用的数据场景，便于下次快速选择"></textarea>
          </label>
          <p class="text-sub">将保存当前编排的 {{ flow.length }} 个清洗动作</p>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="saveModal.show = false">取消</button>
          <button class="btn btn-primary" :disabled="!saveModal.name.trim() || savingPreset" @click="savePreset">
            {{ savingPreset ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
  `,
  setup() {
    // ---------- 通用 ----------
    const toast = reactive({ show: false, msg: '', type: 'info' });
    let toastTimer = null;
    function showToast(msg, type = 'info') {
      toast.show = true; toast.msg = msg; toast.type = type;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toast.show = false; }, 3200);
    }

    async function api(path, opts) {
      const res = await fetch(path, opts);
      try { return await res.json(); }
      catch { return { code: -1, msg: '服务响应异常' }; }
    }

    function fmtNum(n) { return n == null ? '—' : Number(n).toLocaleString('zh-CN'); }
    function typeText(t) { return t === 'csv' ? 'CSV' : 'Excel'; }
    function showSpace(v) { return String(v).replace(/ /g, '␠'); }
    const ANOMALY_PAGE_CONST = ANOMALY_PAGE;

    const actionDefs = ACTION_DEFS;
    function actionLabel(t) { return ACTION_LABEL[t] || t; }

    // ---------- 文件列表 ----------
    const mode = ref('list');
    const list = ref([]);
    const loading = ref(false);

    async function loadList() {
      loading.value = true;
      const body = await api('/api/files?page=1&page_size=100');
      loading.value = false;
      if (body.code !== 0) { showToast(body.msg || '文件列表加载失败', 'error'); return; }
      list.value = body.data.list;
    }

    // ---------- 报告 / 流程 / 记录 ----------
    const file = ref({});
    const report = ref(null);
    const running = ref(false);
    const tab = ref('profile');
    const dupCols = ref([]);
    const dupView = ref('full');
    const showDup = ref(false);
    const selectedIds = ref([]);
    const pendingSuggestions = ref([]);

    const columns = computed(() => (report.value ? report.value.columns : (file.value.columns || [])));

    const s = computed(() => report.value ? report.value.summary : {});
    const curDup = computed(() => {
      const r = report.value;
      if (!r) return { samples: [] };
      return (dupView.value === 'sub' && r.duplicates.by_columns)
        ? r.duplicates.by_columns : r.duplicates.full;
    });
    const visibleAnomalies = computed(() =>
      report.value ? report.value.anomalies.items.slice(0, ANOMALY_PAGE_CONST) : []);
    const confirmedCnt = computed(() =>
      report.value ? report.value.anomalies.items.filter(a => a.confirmed).length : 0);
    const allVisibleChecked = computed(() =>
      visibleAnomalies.value.length > 0
      && visibleAnomalies.value.every(a => a.confirmed || selectedIds.value.includes(a.id)));
    const pendingSuggestionsText = computed(() =>
      pendingSuggestions.value.map(x => x.field + '（' + (x.kind === 'number' ? '转数值' : '转日期') + '）').join('、'));

    async function openReport(id) {
      mode.value = 'report';
      tab.value = 'profile';
      report.value = null;
      loading.value = true;
      dupCols.value = [];
      selectedIds.value = [];
      pendingSuggestions.value = [];
      flow.value = [];
      currentPreset.value = null;
      const body = await api('/api/files/' + id + '/profile');
      loading.value = false;
      if (body.code !== 0) { showToast(body.msg || '报告加载失败', 'error'); mode.value = 'list'; return; }
      file.value = body.data.file;
      if (body.data.exists) {
        report.value = body.data.report;
        dupView.value = body.data.report.duplicates.by_columns ? 'sub' : 'full';
      }
    }

    function backToList() {
      mode.value = 'list';
      report.value = null;
      loadList();
    }

    async function runProfile(cols) {
      if (running.value) return;
      running.value = true;
      const payload = (cols && cols.length) ? { columns: cols } : {};
      const body = await api('/api/files/' + file.value.id + '/profile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      running.value = false;
      if (body.code !== 0) { showToast(body.msg || '体检失败', 'error'); return; }
      report.value = body.data.report;
      file.value.status = '体检完成';
      selectedIds.value = [];
      dupView.value = (cols && cols.length) ? 'sub' : 'full';
      showToast('体检完成，耗时 ' + body.data.report.duration_sec + ' 秒', 'success');
    }

    // ---------- 异常确认 ----------
    function toggleOne(a) {
      const i = selectedIds.value.indexOf(a.id);
      if (i >= 0) selectedIds.value.splice(i, 1);
      else selectedIds.value.push(a.id);
    }
    function toggleAllVisible(e) {
      const checked = e.target.checked;
      const ids = visibleAnomalies.value.filter(a => !a.confirmed).map(a => a.id);
      if (checked) {
        const set = new Set(selectedIds.value.concat(ids));
        selectedIds.value = Array.from(set);
      } else {
        const idset = new Set(ids);
        selectedIds.value = selectedIds.value.filter(x => !idset.has(x));
      }
    }
    async function confirmIds(ids) {
      if (!ids.length) { showToast('请先勾选要确认的异常项', 'info'); return; }
      const body = await api('/api/files/' + file.value.id + '/profile/anomaly-confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      if (body.code !== 0) { showToast(body.msg || '确认失败', 'error'); return; }
      const idset = new Set(ids);
      report.value.anomalies.items.forEach(a => { if (idset.has(a.id)) a.confirmed = true; });
      selectedIds.value = selectedIds.value.filter(x => !idset.has(x));
      showToast('已确认 ' + body.data.confirmed + ' 条异常项', 'success');
    }
    function confirmSelected() { confirmIds(selectedIds.value.slice()); }
    function confirmAll() {
      const ids = report.value.anomalies.items.map(a => a.id);
      confirmIds(ids);
    }

    // ---------- 类型建议 → 清洗流程 ----------
    function joinFlow(sg) {
      if (!pendingSuggestions.value.some(x => x.id === sg.id)) {
        pendingSuggestions.value.push(sg);
      }
      const action = sg.kind === 'number'
        ? { type: 'convert_type', field: sg.field, target: 'number' }
        : { type: 'normalize_date', field: sg.field, output_format: 'YYYY-MM-DD' };
      addAction(action.type, action);
      tab.value = 'flow';
      showToast('已将「' + sg.field + '」的转换建议加入清洗流程', 'success');
    }

    // ---------- 清洗流程编排 ----------
    const flow = ref([]);
    const previewing = ref(false);
    const executing = ref(false);
    const resultModal = reactive({ show: false, output_name: '', stats: {} });
    let _aid = 0;

    function defaultParams(type) {
      const d = ACTION_DEFS.find(x => x.type === type);
      return d ? JSON.parse(JSON.stringify(d.defaultParams)) : {};
    }

    function addAction(type, preset) {
      const params = defaultParams(type);
      if (preset) Object.assign(params, preset);
      flow.value.push({
        id: ++_aid,
        type,
        params,
        confirmed: false,
        preview: null,
      });
    }

    function removeAction(i) { flow.value.splice(i, 1); }

    function moveAction(i, delta) {
      const j = i + delta;
      if (j < 0 || j >= flow.value.length) return;
      const tmp = flow.value[i];
      flow.value.splice(i, 1);
      flow.value.splice(j, 0, tmp);
    }

    // 拖拽排序
    const dragIndex = ref(null);
    function onDragStart(i) { dragIndex.value = i; }
    function onDragOver(i) { dragIndex.value = i; }
    function onDrop(i) {
      const from = dragIndex.value;
      if (from == null || from === i) return;
      const item = flow.value.splice(from, 1)[0];
      flow.value.splice(i, 0, item);
      dragIndex.value = null;
    }
    function onDragEnd() { dragIndex.value = null; }

    function isDestructive(a) {
      return ACTION_DESTRUCTIVE[a.type] || actionDeletesRows(a);
    }
    function confirmDesc(a) {
      if (a.type === 'drop_field') return '将删除该字段的全部数据';
      if (a.type === 'fill_missing') return '将删除缺失行';
      if (a.type === 'filter_rows') return '将删除不满足条件的行';
      if (a.type === 'handle_anomaly') return '将剔除异常行';
      return '可能删除数据';
    }

    const unconfirmedCount = computed(() =>
      flow.value.filter(a => isDestructive(a) && !a.confirmed).length);

    const canExecute = computed(() =>
      flow.value.length > 0 && unconfirmedCount.value === 0);

    // filter_rows 条件增删
    function addFilterCond(a) {
      a.params.conditions.push({ field: '', operator: '=', value: '' });
    }
    function removeFilterCond(a, ci) {
      a.params.conditions.splice(ci, 1);
    }

    // 异常行号提示（来自体检报告）
    const anomalyRowHint = computed(() => {
      if (!report.value) return '行号，如 12,50,120';
      const items = report.value.anomalies.items || [];
      if (!items.length) return '无异常行';
      const sample = items.slice(0, 10).map(a => a.row_no).join(',');
      return '例：' + sample + (items.length > 10 ? '…' : '');
    });

    function buildAnomalyRows(a) {
      const raw = (a.params.anomaly_rows || '').trim();
      if (raw) {
        return raw.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n));
      }
      // 留空则由后端按字段规则自动检测异常行（避免前置删行动作导致行号错位）
      return [];
    }

    // 序列化动作（供后端调用）
    function serializeActions() {
      return flow.value.map(a => {
        const params = JSON.parse(JSON.stringify(a.params));
        // drop_duplicates columns 字符串转数组
        if (a.type === 'drop_duplicates') {
          const cols = (params.columns || '').toString().trim();
          params.columns = cols ? cols.split(',').map(s => s.trim()).filter(Boolean) : null;
        }
        // handle_anomaly 行号
        if (a.type === 'handle_anomaly') {
          params.anomaly_rows = buildAnomalyRows(a);
        }
        return { type: a.type, params };
      });
    }

    async function previewAction(a) {
      previewing.value = true;
      const payload = { file_id: file.value.id, actions: serializeActions() };
      const body = await api('/api/clean/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      previewing.value = false;
      if (body.code !== 0) {
        a.preview = { ok: false, error: body.msg || '预览失败' };
        showToast(body.msg || '预览失败', 'error');
        return;
      }
      // 找到本动作在返回结果中的位置
      const idx = flow.value.findIndex(x => x.id === a.id);
      const res = body.data.actions[idx];
      a.preview = res || { ok: false, error: '无预览结果' };
    }

    async function executeFlow() {
      if (!canExecute.value) return;
      executing.value = true;
      const payload = {
        file_id: file.value.id,
        actions: serializeActions(),
        preset_id: currentPreset.value ? currentPreset.value.id : null,
      };
      const body = await api('/api/clean/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      executing.value = false;
      if (body.code !== 0) { showToast(body.msg || '执行失败', 'error'); return; }
      resultModal.output_name = body.data.output_name;
      resultModal.stats = body.data.stats;
      resultModal.show = true;
      file.value.status = '清洗完成';
      showToast('清洗完成，产物：' + body.data.output_name, 'success');
    }

    // ---------- 清洗预设（卡05） ----------
    const presets = ref([]);
    const currentPreset = ref(null);   // 当前流程来源预设 { id, name, is_builtin }
    const presetModal = reactive({ show: false, loading: false, file_id: null, file_name: '' });
    const saveModal = reactive({ show: false, name: '', scenario: '' });
    const savingPreset = ref(false);

    async function loadPresets() {
      const body = await api('/api/clean/presets');
      if (body.code !== 0) { showToast(body.msg || '预设列表加载失败', 'error'); return; }
      presets.value = body.data.presets;
    }

    // 清洗入口：先弹预设选择（含空白流程）
    async function startClean(item) {
      if (!item || !item.id) return;
      presetModal.file_id = item.id;
      presetModal.file_name = item.origin_name || ('文件 #' + item.id);
      presetModal.loading = true;
      presetModal.show = true;
      await loadPresets();
      presetModal.loading = false;
    }

    // 预设动作 → 编排区（数组参数反序列化为前端编辑格式）
    function loadActionsIntoFlow(actions) {
      flow.value = [];
      actions.forEach(a => {
        const params = JSON.parse(JSON.stringify(a.params || {}));
        if (a.type === 'drop_duplicates' && Array.isArray(params.columns)) {
          params.columns = params.columns.join(', ');
        }
        if (a.type === 'handle_anomaly') {
          params.anomaly_rows = (Array.isArray(params.anomaly_rows) && params.anomaly_rows.length)
            ? params.anomaly_rows.join(',') : '';
        }
        flow.value.push({ id: ++_aid, type: a.type, params, confirmed: false, preview: null });
      });
    }

    async function choosePreset(p) {
      const fid = presetModal.file_id;
      presetModal.show = false;
      const body = await api('/api/clean/presets/' + p.id + '/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_id: fid }),
      });
      if (body.code !== 0) { showToast(body.msg || '套用预设失败', 'error'); return; }
      await openReport(fid);
      loadActionsIntoFlow(body.data.actions);
      tab.value = 'flow';
      currentPreset.value = {
        id: body.data.preset_id, name: body.data.preset_name, is_builtin: body.data.is_builtin,
      };
      showToast('已套用预设「' + body.data.preset_name + '」（' + body.data.actions.length + ' 个动作），可微调后执行', 'success');
    }

    async function chooseBlankFlow() {
      const fid = presetModal.file_id;
      presetModal.show = false;
      await openReport(fid);
      tab.value = 'flow';
      currentPreset.value = null;
      showToast('已进入空白流程，从左侧添加清洗动作', 'info');
    }

    async function deletePreset(p) {
      if (!confirm('确认删除自建预设「' + p.name + '」？')) return;
      const body = await api('/api/clean/presets/' + p.id, { method: 'DELETE' });
      if (body.code !== 0) { showToast(body.msg || '删除失败', 'error'); return; }
      showToast('已删除预设「' + p.name + '」', 'success');
      loadPresets();
    }

    function openSavePreset() {
      if (!flow.value.length) { showToast('请先编排至少一个清洗动作', 'info'); return; }
      saveModal.name = '';
      saveModal.scenario = '';
      saveModal.show = true;
    }

    async function savePreset() {
      const name = saveModal.name.trim();
      if (!name) return;
      savingPreset.value = true;
      const body = await api('/api/clean/presets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          scenario: saveModal.scenario.trim(),
          actions: serializeActions(),
        }),
      });
      savingPreset.value = false;
      if (body.code !== 0) { showToast(body.msg || '保存失败', 'error'); return; }
      saveModal.show = false;
      currentPreset.value = { id: body.data.preset_id, name, is_builtin: 0 };
      showToast('已保存为预设「' + name + '」，下次清洗可直接套用', 'success');
      loadPresets();
    }

    // 变更统计摘要（时间线卡片用）
    function recordSummary(st) {
      st = st || {};
      const parts = [];
      if (st.missing_filled > 0) parts.push('处理缺失 ' + fmtNum(st.missing_filled) + ' 处');
      if (st.rows_dropped_dedup > 0) parts.push('去重 ' + fmtNum(st.rows_dropped_dedup) + ' 行');
      const conv = st.fields_converted_cnt != null ? st.fields_converted_cnt : (st.fields_converted || []).length;
      if (conv > 0) parts.push('类型转换 ' + conv + ' 个字段');
      if (st.rows_dropped_anomaly > 0) parts.push('剔除异常 ' + fmtNum(st.rows_dropped_anomaly) + ' 行');
      if (st.rows_dropped_missing > 0) parts.push('删缺失行 ' + fmtNum(st.rows_dropped_missing) + ' 行');
      if (st.rows_dropped_filter > 0) parts.push('筛选删除 ' + fmtNum(st.rows_dropped_filter) + ' 行');
      if (st.strip_count > 0) parts.push('修剪空格 ' + fmtNum(st.strip_count) + ' 处');
      if (!parts.length) parts.push('无行级明细变更');
      return parts;
    }

    function gotoOutput() {
      // 跳转到数据存储视图查看产物（app.js 监听 liteops:goto）
      window.dispatchEvent(new CustomEvent('liteops:goto', { detail: 'files' }));
    }

    // ---------- 清洗记录 ----------
    const records = ref([]);
    const recordsLoading = ref(false);

    async function loadRecords() {
      recordsLoading.value = true;
      const body = await api('/api/clean/records?file_id=' + file.value.id);
      recordsLoading.value = false;
      if (body.code !== 0) { showToast(body.msg || '记录加载失败', 'error'); return; }
      records.value = body.data.records;
    }

    async function rollbackRecord(r) {
      if (!confirm('确认回滚该清洗记录？将删除产物文件并恢复原文件状态。')) return;
      const body = await api('/api/clean/records/' + r.id + '/rollback', { method: 'POST' });
      if (body.code !== 0) { showToast(body.msg || '回滚失败', 'error'); return; }
      showToast('已回滚清洗记录', 'success');
      loadRecords();
    }

    onMounted(() => { loadList(); });

    return {
      toast, mode, list, loading,
      file, report, running, tab, dupCols, dupView, showDup,
      selectedIds, pendingSuggestions, pendingSuggestionsText,
      s, curDup, visibleAnomalies, confirmedCnt, allVisibleChecked,
      ANOMALY_PAGE,
      openReport, backToList, runProfile,
      toggleOne, toggleAllVisible, confirmSelected, confirmAll,
      joinFlow,
      fmtNum, typeText, showSpace,
      // 卡04
      actionDefs, actionLabel, flow, previewing, executing, resultModal,
      columns, unconfirmedCount, canExecute, isDestructive, confirmDesc,
      anomalyRowHint, dragIndex,
      records, recordsLoading,
      addAction, removeAction, moveAction,
      onDragStart, onDragOver, onDrop, onDragEnd,
      addFilterCond, removeFilterCond,
      previewAction, executeFlow, loadRecords, rollbackRecord,
      // 卡05 预设与统计
      presets, currentPreset, presetModal, saveModal, savingPreset,
      startClean, choosePreset, chooseBlankFlow, deletePreset,
      openSavePreset, savePreset, recordSummary, gotoOutput,
    };
  },
};
})();
