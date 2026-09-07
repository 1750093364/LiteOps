/* 报告工作台视图（卡09）。
   列表页：报告卡片（模板类型/关联需求单/引用数/更新时间）+ 新建弹窗（选模板 + 关联需求单）；
   编辑页：左侧 contenteditable 富文本编辑器 + 右侧「数据引用」面板；
     - 工具栏：H1/H2/H3/正文、加粗、有序/无序列表、插入 3×3 表格（execCommand 实现，无第三方库）；
     - 插入框架模板：7 内置模板章节以 H2 标题 + 灰色斜体提示语段落（class=template-hint，供导出识别）追加；
     - 引用数据弹窗：选文件→选字段→选聚合方式→后端 /calc 真实计算→插入绿色引用卡片
       （contenteditable=false，data-ref 属性存 JSON，refs_json 同步记录来源）；
     - 图片粘贴：base64 内嵌，单张限制 2MB；
     - 30 秒防抖自动保存（持续输入时 30s 节流兜底），顶部显示「已保存 HH:MM:SS」；
     - 监听 liteops:insert-snippet：片段库 SQL 在光标处插入 pre 代码块（跨视图经 sessionStorage 暂存）。
   Vue3 全局构建，无构建步骤。整体包在 IIFE 内，避免全局 const 冲突。 */
(function () {
const { ref, reactive, computed, onMounted, onUnmounted, nextTick } = Vue;

const AGG_OPTIONS = [
  { key: 'count', label: '计数（行数）' },
  { key: 'sum', label: '合计' },
  { key: 'mean', label: '平均值' },
  { key: 'max', label: '最大值' },
  { key: 'min', label: '最小值' },
  { key: 'count_nonnull', label: '非空计数' },
];
const AUTOSAVE_MS = 30000;              // 30 秒防抖自动保存
const MAX_IMG_BYTES = 2 * 1024 * 1024;  // 粘贴图片单张上限 2MB
const PENDING_SNIPPET_KEY = 'liteops:pending-snippet';

function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function fmtNum(v) {
  if (typeof v === 'number' && isFinite(v)) {
    return v.toLocaleString('zh-CN', { maximumFractionDigits: 4 });
  }
  return String(v);
}

window.ReportsView = {
  name: 'ReportsView',
  template: `
  <div class="rep-view">
    <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.msg }}</div>

    <!-- ========== 报告列表 ========== -->
    <div v-if="mode === 'list'">
      <div class="view-head">
        <div>
          <h1>报告工作台</h1>
          <p>框架模板引导写作，数据引用卡片让每个数字都有来源；30 秒自动保存草稿</p>
        </div>
        <button class="btn btn-primary" @click="openCreate">＋ 新建报告</button>
      </div>

      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="list.length === 0" class="empty">
        暂无报告，点击「新建报告」选择一个分析框架模板开始
      </div>
      <div v-else class="rep-list">
        <div v-for="r in list" :key="r.id" class="rep-card"
             @click="openReport(r)">
          <div class="rep-card-main">
            <div class="rep-card-title">
              <span class="tk-title">{{ r.title }}</span>
              <span class="badge hint" v-if="r.template_type">模板·{{ r.template_type }}</span>
              <span class="badge status">{{ r.status || '草稿' }}</span>
            </div>
            <div class="rep-card-sub">
              <span v-if="ticketTitle(r.ticket_id)">🎫 关联需求：{{ ticketTitle(r.ticket_id) }}</span>
              <span>📊 数据引用 {{ r.refs_cnt }} 处</span>
              <span>🕐 更新于 {{ r.updated_at }}</span>
            </div>
          </div>
          <div class="rep-card-ops" @click.stop>
            <button class="btn btn-mini btn-primary" @click="openReport(r)">打开编辑</button>
            <button class="btn btn-mini btn-danger" @click="removeReport(r)">删除</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ========== 报告编辑页 ========== -->
    <div v-else class="rep-editor-wrap">
      <div class="rep-topbar">
        <button class="btn btn-mini" @click="backToList">← 返回列表</button>
        <input class="rep-title-input" v-model="current.title"
               placeholder="报告标题" @input="onInput">
        <span class="rep-save-status" :class="saveState">
          {{ saveState === 'saved' ? '已保存 ' + savedAtText
             : (saveState === 'saving' ? '保存中…' : '有未保存更改') }}
        </span>
        <button class="btn btn-mini" @click="saveNow">立即保存</button>
      </div>

      <div class="rep-actions">
        <button class="btn btn-mini btn-primary" @click="openTplModal">📋 插入框架模板</button>
        <button class="btn btn-mini" @click="openRefDialog">📊 引用数据</button>
        <span class="rep-tb-sep"></span>
        <button class="btn btn-mini btn-ai" @click="openAiFramework" :disabled="aiBusy"
                :title="aiReady ? 'AI 根据主题生成章节框架（不编造数字）' : '未配置 Key 或处于离线模式，请到「设置」配置'">
          🤖 AI 构架
        </button>
        <button class="btn btn-mini btn-ai" @click="openAiPolish" :disabled="aiBusy"
                :title="aiReady ? '选中段落或全文做语言精炼（数字一个不丢）' : '未配置 Key 或处于离线模式，请到「设置」配置'">
          ✨ AI 精炼
        </button>
        <span class="rep-tb-sep"></span>
        <button class="btn btn-mini" @click="beginExport('word')">📄 导出 Word</button>
        <button class="btn btn-mini" @click="beginExport('pdf')">🖨 导出 PDF</button>
        <span class="rep-tip">正文可直接粘贴截图（≤2MB） · 导出前需核对数据引用</span>
      </div>

      <div class="ai-unsourced-banner" v-if="unsourcedCnt > 0">
        ⛔ 正文存在 <b>{{ unsourcedCnt }}</b> 处<b>无数据来源数字</b>（红色高亮，鼠标悬停可见提示），
        请核对或删除后才能导出——导出已被拦截
      </div>

      <div class="rep-toolbar" @mousedown.prevent>
        <button class="btn btn-mini" @click="fmtBlock('H1')">H1</button>
        <button class="btn btn-mini" @click="fmtBlock('H2')">H2</button>
        <button class="btn btn-mini" @click="fmtBlock('H3')">H3</button>
        <button class="btn btn-mini" @click="fmtBlock('P')">正文</button>
        <span class="rep-tb-sep"></span>
        <button class="btn btn-mini rep-tb-bold" @click="execCmd('bold')">B</button>
        <span class="rep-tb-sep"></span>
        <button class="btn btn-mini" @click="execCmd('insertOrderedList')">1. 有序列表</button>
        <button class="btn btn-mini" @click="execCmd('insertUnorderedList')">• 无序列表</button>
        <span class="rep-tb-sep"></span>
        <button class="btn btn-mini" @click="insertTable">插入表格 3×3</button>
      </div>

      <div class="rep-layout">
        <div class="report-editor" ref="editorEl" contenteditable="true"
             data-placeholder="从这里开始撰写报告：可先「插入框架模板」生成章节骨架，再用「引用数据」插入真实数字…"
             @input="onInput" @paste="onPaste" @blur="saveCaret"
             @keydown="onKeydown"></div>

        <aside class="rep-refs-panel">
          <div class="rep-refs-head">
            <b>数据引用</b>
            <span class="badge hint">{{ current.refs.length }} 处</span>
          </div>
          <div v-if="current.refs.length === 0" class="rep-refs-empty">
            点击上方「引用数据」插入后端真实计算的数字卡片，每个数字自动记录来源文件与字段
          </div>
          <div v-for="(rf, i) in current.refs" :key="i" class="rep-ref-item">
            <div class="rep-ref-val">{{ fmtNum(rf.value) }}</div>
            <div class="rep-ref-src">{{ rf.file_name }} · {{ rf.field }} · {{ rf.agg_label }}</div>
          </div>
        </aside>
      </div>
    </div>

    <!-- ========== 新建报告弹窗 ========== -->
    <div class="modal-mask" v-if="createModal.show" @click.self="createModal.show = false">
      <div class="modal">
        <h3>新建报告</h3>
        <label class="fm-label">报告标题
          <input v-model="createModal.title" placeholder="如：双11 大促活动复盘">
        </label>
        <label class="fm-label">分析框架模板（新建后自动插入章节骨架）
          <select class="cat-select" v-model="createModal.template_type">
            <option value="">空白报告（不插入模板）</option>
            <option v-for="t in templates" :key="t.id" :value="t.type">
              {{ t.name }}（{{ t.type }}）
            </option>
          </select>
        </label>
        <label class="fm-label">关联需求单（可选）
          <select class="cat-select" v-model="createModal.ticket_id">
            <option :value="null">不关联</option>
            <option v-for="t in tickets" :key="t.id" :value="t.id">
              #{{ t.id }} {{ t.title }}（{{ t.status_label }}）
            </option>
          </select>
        </label>
        <div class="modal-foot">
          <button class="btn" @click="createModal.show = false">取消</button>
          <button class="btn btn-primary" :disabled="creating" @click="submitCreate">
            {{ creating ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ========== 插入框架模板弹窗 ========== -->
    <div class="modal-mask" v-if="tplModal.show" @click.self="tplModal.show = false">
      <div class="modal">
        <h3>插入框架模板</h3>
        <p class="rep-modal-tip">模板章节将以 H2 标题 + 灰色提示语追加到文末，提示语可按需改写或删除</p>
        <div class="rep-tpl-list">
          <label v-for="t in templates" :key="t.id" class="rep-tpl-item"
                 :class="{ active: tplModal.type === t.type }">
            <input type="radio" :value="t.type" v-model="tplModal.type">
            <div>
              <b>{{ t.name }}</b>
              <div class="rep-tpl-secs">{{ t.sections.map(s => s.title).join(' → ') }}</div>
            </div>
          </label>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="tplModal.show = false">取消</button>
          <button class="btn btn-primary" :disabled="!tplModal.type" @click="confirmInsertTemplate">
            插入到文末
          </button>
        </div>
      </div>
    </div>

    <!-- ========== 引用数据弹窗 ========== -->
    <div class="modal-mask" v-if="refDlg.show" @click.self="refDlg.show = false">
      <div class="modal">
        <h3>引用数据（后端真实计算）</h3>
        <label class="fm-label">① 选择数据文件
          <select class="cat-select" v-model="refDlg.file_id" @change="onRefFileChange">
            <option :value="null" disabled>请选择文件</option>
            <option v-for="f in files" :key="f.id" :value="f.id">
              {{ f.origin_name }}（{{ f.row_count }} 行）
            </option>
          </select>
        </label>
        <label class="fm-label">② 选择字段
          <select class="cat-select" v-model="refDlg.field" :disabled="!refDlg.file_id">
            <option value="" disabled>请选择字段</option>
            <option v-for="f in refFields" :key="f.field_name" :value="f.field_name">
              {{ f.field_name }}（{{ f.inferred_type }}）
            </option>
          </select>
        </label>
        <label class="fm-label">③ 聚合方式
          <select class="cat-select" v-model="refDlg.agg">
            <option v-for="a in aggOptions" :key="a.key" :value="a.key">{{ a.label }}</option>
          </select>
        </label>
        <div class="rep-calc-result" v-if="refDlg.result">
          计算结果：<b>{{ fmtNum(refDlg.result.value) }}</b>
          <span class="rep-calc-sub">
            （{{ refDlg.result.file_name }} · {{ refDlg.result.field }} {{ refDlg.result.agg_label }}，
            基于 {{ refDlg.result.rows_used }} 行）
          </span>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="refDlg.show = false">取消</button>
          <button class="btn btn-primary" :disabled="!canCalc" @click="doCalc">
            {{ refDlg.loading ? '计算中…' : '计算并插入' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ========== 导出数据引用核对清单弹窗（卡10） ========== -->
    <div class="modal-mask" v-if="expModal.show" @click.self="expModal.show = false">
      <div class="modal exp-modal">
        <h3>数据引用核对清单</h3>
        <p class="rep-modal-tip">导出前请逐条核对每个引用数字的来源，全部勾选后方可导出（防幻觉硬约束）</p>
        <div class="exp-check-head">
          <label class="exp-all">
            <input type="checkbox" :checked="expAllChecked" @change="toggleAllRefs"> 全选
          </label>
          <span class="exp-cnt">已勾选 {{ expCheckedCnt }} / {{ expModal.refs.length }}</span>
        </div>
        <div class="exp-table-wrap">
          <table class="exp-table">
            <thead>
              <tr><th style="width:44px">核对</th><th>数值</th><th>来源文件</th><th>字段</th><th>聚合方式</th></tr>
            </thead>
            <tbody>
              <tr v-if="expModal.refs.length === 0">
                <td colspan="5" class="exp-empty">本报告暂无数据引用卡片，可直接导出</td>
              </tr>
              <tr v-for="(rf, i) in expModal.refs" :key="i">
                <td class="exp-chk"><input type="checkbox" v-model="rf.checked"></td>
                <td class="exp-val">{{ fmtNum(rf.value) }}</td>
                <td>{{ rf.file_name }}</td>
                <td>{{ rf.field }}</td>
                <td>{{ rf.agg_label }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="exp-warn" v-if="!expAllChecked">⚠ 请逐条核对数据来源</div>
        <div class="modal-foot">
          <button class="btn" @click="expModal.show = false">取消</button>
          <button class="btn btn-primary" :disabled="!expAllChecked || expLoading"
                  @click="confirmExport('word')">导出 Word</button>
          <button class="btn btn-primary" :disabled="!expAllChecked"
                  @click="confirmExport('pdf')">导出 PDF</button>
        </div>
      </div>
    </div>

    <!-- ========== AI 构架弹窗（卡11） ========== -->
    <div class="modal-mask" v-if="fwModal.show" @click.self="fwModal.show = false">
      <div class="modal ai-modal">
        <h3>🤖 AI 构架（生成章节框架）</h3>
        <p class="rep-modal-tip">
          AI 只生成章节标题与写作要点，<b>不会编造数字</b>；要点中若出现红色数字＝无数据来源，
          插入后请核对或删除。发送内容仅含主题文字、字段名与已引用数值，<b>不含任何数据行</b>。
        </p>
        <label class="fm-label">报告主题 / 需求描述
          <textarea v-model="fwModal.topic" rows="3"
            placeholder="如：双11 大促活动复盘，重点分析各渠道转化率与客单价变化，结论用于下周渠道预算分配"></textarea>
        </label>
        <label class="fm-label">模板类型
          <select class="cat-select" v-model="fwModal.template_type">
            <option value="">通用数据分析报告</option>
            <option v-for="t in templates" :key="t.id" :value="t.type">{{ t.name }}（{{ t.type }}）</option>
          </select>
        </label>

        <div class="ai-fields-box">
          <div class="ai-fields-head">
            <b>参与字段（仅发送字段名）</b>
            <select class="cat-select ai-file-select" v-model="fwModal.file_id"
                    @change="aiAddFields($event.target.value)">
              <option :value="null">＋ 从数据文件添加字段…</option>
              <option v-for="f in files" :key="f.id" :value="f.id">{{ f.origin_name }}</option>
            </select>
          </div>
          <div v-if="aiFields.length === 0" class="ai-fields-empty">
            未添加字段（可不选）；勾选「脱敏」的字段名在请求中会被替换为 [已脱敏字段]
          </div>
          <label v-for="(fld, i) in aiFields" :key="i" class="ai-field-item">
            <input type="checkbox" v-model="fld.masked" title="勾选后该字段名脱敏后再发送">
            <span class="ai-field-name">{{ fld.field_name }}</span>
            <span class="ai-field-meta">{{ fld.file_name }} · {{ fld.inferred_type }}</span>
            <button class="ai-field-del" @click.prevent="aiFields.splice(i, 1)">✕</button>
          </label>
        </div>

        <div class="ai-gen-result" v-if="fwModal.html">
          <div class="ai-result-head">
            <b>框架预览</b>
            <span class="badge" :class="fwModal.unsourced > 0 ? 'status' : 'hint'">
              {{ fwModal.unsourced > 0 ? ('⚠ ' + fwModal.unsourced + ' 处无来源数字') : '✓ 无外造数字' }}
            </span>
          </div>
          <div class="ai-preview" v-html="fwModal.html"></div>
        </div>

        <div class="modal-foot">
          <button class="btn" @click="fwModal.show = false">取消</button>
          <button class="btn" :disabled="fwModal.loading || !fwModal.topic.trim()" @click="doAiFramework">
            {{ fwModal.loading ? '生成中（最多 30 秒）…' : (fwModal.html ? '重新生成' : '生成框架') }}
          </button>
          <button class="btn btn-primary" :disabled="!fwModal.html || fwModal.loading"
                  @click="insertAiFramework">一键插入编辑器</button>
        </div>
      </div>
    </div>

    <!-- ========== AI 精炼弹窗（卡11） ========== -->
    <div class="modal-mask" v-if="polModal.show" @click.self="polModal.show = false">
      <div class="modal ai-modal">
        <h3>✨ AI 精炼（语言优化，数字一个不丢）</h3>
        <p class="rep-modal-tip">
          强约束：AI 必须保留原文全部数字、日期、专有名词，且<b>不得新增任何数字</b>；
          新增数字会<b>标红</b>，丢失数字会<b>告警</b>。超时/报错不改动原文，可重试。
        </p>
        <div class="ai-polish-modes">
          <label><input type="radio" value="professional" v-model="polModal.mode"> 专业化（书面严谨）</label>
          <label><input type="radio" value="structured" v-model="polModal.mode"> 结构化（分层分点）</label>
          <span class="ai-scope-tag">
            {{ polModal.scope === 'selection' ? '选段精炼' : '全文精炼' }} · {{ polModal.origText.length }} 字
          </span>
        </div>
        <div class="ai-warn-line" v-if="polModal.scope === 'full'">
          ⚠ 全文精炼结果为纯文本，接受后将替换整篇正文（引用卡片/图片/表格会变为文本）；建议优先选段精炼。
        </div>

        <div class="ai-fields-box">
          <div class="ai-fields-head">
            <b>脱敏字段（可选）</b>
            <select class="cat-select ai-file-select" v-model="polModal.file_id"
                    @change="aiAddFields($event.target.value)">
              <option :value="null">＋ 从数据文件添加字段名用于脱敏…</option>
              <option v-for="f in files" :key="f.id" :value="f.id">{{ f.origin_name }}</option>
            </select>
          </div>
          <label v-for="(fld, i) in aiFields" :key="i" class="ai-field-item">
            <input type="checkbox" v-model="fld.masked" title="勾选后该字段名脱敏后再发送">
            <span class="ai-field-name">{{ fld.field_name }}</span>
            <span class="ai-field-meta">{{ fld.file_name }} · {{ fld.inferred_type }}</span>
            <button class="ai-field-del" @click.prevent="aiFields.splice(i, 1)">✕</button>
          </label>
        </div>

        <div class="ai-diff" v-if="polModal.html">
          <div class="ai-diff-col">
            <div class="ai-diff-head">原文（未改动）</div>
            <div class="ai-diff-body">{{ polModal.origText }}</div>
          </div>
          <div class="ai-diff-col">
            <div class="ai-diff-head">
              精炼结果
              <span class="badge" :class="polModal.unsourced > 0 ? 'status' : 'hint'">
                {{ polModal.unsourced > 0 ? ('⚠ ' + polModal.unsourced + ' 处无来源') : '✓ 无外造数字' }}
              </span>
            </div>
            <div class="ai-diff-body ai-diff-new" v-html="polModal.html"></div>
          </div>
        </div>
        <div class="ai-warn-line" v-if="polModal.dropped.length">
          ⚠ 原文 {{ polModal.dropped.length }} 个数字在精炼结果中未找到：
          <b>{{ polModal.dropped.join('、') }}</b>，建议放弃后重试
        </div>

        <div class="modal-foot">
          <button class="btn" @click="polModal.show = false">放弃</button>
          <button class="btn" :disabled="polModal.loading || !polModal.origText" @click="doAiPolish">
            {{ polModal.loading ? '精炼中（最多 60 秒）…' : (polModal.html ? '重新精炼' : '开始精炼') }}
          </button>
          <button class="btn btn-primary" :disabled="!polModal.html || polModal.loading"
                  @click="acceptAiPolish">接受结果</button>
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
      let res;
      try { res = await fetch(path, opts); }
      catch (e) { return { code: -1, msg: '网络请求失败，请确认服务已启动后重试' }; }
      if (!res.ok) {
        return { code: res.status, msg: '服务响应异常（HTTP ' + res.status + '），请重启服务后重试' };
      }
      try { return await res.json(); }
      catch (e) { return { code: -1, msg: '服务响应异常' }; }
    }

    // ---------- 列表 ----------
    const mode = ref('list');
    const list = ref([]);
    const tickets = ref([]);
    const templates = ref([]);
    const files = ref([]);
    const loading = ref(false);
    const creating = ref(false);
    const aggOptions = AGG_OPTIONS;

    function ticketTitle(id) {
      if (!id) return '';
      const t = tickets.value.find(x => x.id === id);
      return t ? t.title : ('需求 #' + id);
    }

    async function loadList() {
      loading.value = true;
      const body = await api('/api/reports');
      loading.value = false;
      if (body.code !== 0) { showToast(body.msg || '报告列表加载失败', 'error'); return; }
      list.value = body.data.list;
    }
    async function loadTickets() {
      const body = await api('/api/tickets');
      if (body.code === 0) tickets.value = body.data.list;
    }
    async function loadTemplates() {
      const body = await api('/api/report-templates');
      if (body.code === 0) templates.value = body.data.list;
    }
    async function loadFiles() {
      const body = await api('/api/files?page=1&page_size=100');
      if (body.code === 0) files.value = body.data.list;
    }

    // ---------- 新建报告 ----------
    const createModal = reactive({ show: false, title: '', template_type: '', ticket_id: null });

    function openCreate() {
      Object.assign(createModal, { show: true, title: '', template_type: '', ticket_id: null });
      loadTickets();
    }

    async function submitCreate() {
      if (!createModal.title.trim()) { showToast('请填写报告标题', 'error'); return; }
      creating.value = true;
      const body = await api('/api/reports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: createModal.title.trim(),
          template_type: createModal.template_type || null,
          ticket_id: createModal.ticket_id,
        }),
      });
      creating.value = false;
      if (body.code !== 0) { showToast(body.msg || '创建失败', 'error'); return; }
      createModal.show = false;
      showToast('报告已创建' + (body.data.template_type ? '，框架模板将自动插入' : ''), 'success');
      openReport(body.data);
    }

    // ---------- 编辑器状态 ----------
    const current = reactive({
      id: null, title: '', ticket_id: null, template_type: '', status: '草稿', refs: [],
    });
    const editorEl = ref(null);
    const dirty = ref(false);
    const saveState = ref('saved');          // saved | editing | saving
    const savedAtText = ref('');
    let debTimer = null;
    let lastSaveTs = 0;
    let lastRange = null;                    // 编辑器失焦前的光标位置（弹窗操作后恢复）

    function nowHMS() {
      const d = new Date();
      const p = n => String(n).padStart(2, '0');
      return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    }

    function markDirty() {
      dirty.value = true;
      saveState.value = 'editing';
    }

    // 30 秒防抖自动保存；持续输入时每满 30s 节流落盘一次
    function onInput() {
      markDirty();
      clearTimeout(debTimer);
      if (lastSaveTs && Date.now() - lastSaveTs >= AUTOSAVE_MS) {
        saveNow();
        return;
      }
      debTimer = setTimeout(saveNow, AUTOSAVE_MS);
    }

    async function saveNow() {
      if (!current.id) return;
      clearTimeout(debTimer);
      saveState.value = 'saving';
      const payload = {
        title: current.title,
        content_html: editorEl.value ? editorEl.value.innerHTML : '',
        refs_json: JSON.stringify(current.refs),
        template_type: current.template_type || null,
      };
      const body = await api('/api/reports/' + current.id, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (body.code !== 0) {
        saveState.value = 'editing';
        showToast(body.msg || '保存失败', 'error');
        return;
      }
      dirty.value = false;
      lastSaveTs = Date.now();
      saveState.value = 'saved';
      savedAtText.value = nowHMS();
    }

    function flushSave() {
      if (mode.value === 'editor' && current.id && dirty.value) saveNow();
    }

    function onKeydown(e) {
      if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault();
        saveNow();
      }
    }

    // ---------- 光标与插入 ----------
    function saveCaret() {
      const sel = window.getSelection();
      if (sel.rangeCount && editorEl.value &&
          editorEl.value.contains(sel.getRangeAt(0).commonAncestorContainer)) {
        lastRange = sel.getRangeAt(0).cloneRange();
      }
    }

    function ensureCaret() {
      editorEl.value.focus();
      const sel = window.getSelection();
      const inEditor = sel.rangeCount > 0 &&
        editorEl.value.contains(sel.getRangeAt(0).commonAncestorContainer);
      if (inEditor) return;
      if (lastRange && editorEl.value.contains(lastRange.commonAncestorContainer)) {
        sel.removeAllRanges();
        sel.addRange(lastRange);
        return;
      }
      const range = document.createRange();
      range.selectNodeContents(editorEl.value);
      range.collapse(false);  // 光标置末尾
      sel.removeAllRanges();
      sel.addRange(range);
    }

    function insertHtmlAtCursor(html) {
      ensureCaret();
      document.execCommand('insertHTML', false, html);
      saveCaret();
      markDirty();
    }

    function insertNodeAtCursor(node) {
      ensureCaret();
      const sel = window.getSelection();
      const range = sel.getRangeAt(0);
      range.deleteContents();
      range.insertNode(node);
      const tail = document.createTextNode('\u00A0');  // 卡片后补空格，方便继续输入
      node.after(tail);
      range.setStartAfter(tail);
      range.collapse(true);
      sel.removeAllRanges();
      sel.addRange(range);
      saveCaret();
      markDirty();
    }

    // ---------- 工具栏 ----------
    function execCmd(cmd) {
      ensureCaret();
      document.execCommand(cmd, false, null);
      markDirty();
    }
    function fmtBlock(tag) {
      ensureCaret();
      document.execCommand('formatBlock', false, tag);
      markDirty();
    }
    function insertTable() {
      let html = '<table border="1"><tbody>';
      for (let r = 0; r < 3; r++) {
        html += '<tr>';
        for (let c = 0; c < 3; c++) html += '<td>&nbsp;</td>';
        html += '</tr>';
      }
      html += '</tbody></table><p><br></p>';
      insertHtmlAtCursor(html);
      showToast('已插入 3×3 表格', 'success');
    }

    // ---------- 粘贴图片（base64 内嵌 ≤2MB） ----------
    function onPaste(e) {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      for (const it of items) {
        if (it.type && it.type.indexOf('image/') === 0) {
          const f = it.getAsFile();
          if (!f) continue;
          e.preventDefault();
          if (f.size > MAX_IMG_BYTES) {
            showToast('图片超过 2MB（' + (f.size / 1024 / 1024).toFixed(1) + 'MB），请压缩后再粘贴', 'error');
            return;
          }
          const reader = new FileReader();
          reader.onload = () => {
            insertHtmlAtCursor('<img src="' + reader.result + '" style="max-width:100%;">');
            showToast('图片已粘贴（base64 内嵌）', 'success');
          };
          reader.readAsDataURL(f);
          return;
        }
      }
      // 非图片粘贴走浏览器默认行为（保留加粗/表格等结构）
    }

    // ---------- 插入框架模板 ----------
    const tplModal = reactive({ show: false, type: '' });

    function openTplModal() {
      tplModal.type = current.template_type || '';
      tplModal.show = true;
    }

    function buildSectionsHtml(tpl) {
      let html = '';
      tpl.sections.forEach(s => {
        html += '<h2>' + escHtml(s.title) + '</h2>'
              + '<p class="template-hint">' + escHtml(s.hint) + '</p>';
      });
      return html;
    }

    async function confirmInsertTemplate() {
      const tpl = templates.value.find(t => t.type === tplModal.type);
      if (!tpl) return;
      tplModal.show = false;
      // 章节「追加」到文末（空文档则直接填入）
      if (!editorEl.value.innerHTML.trim()) {
        editorEl.value.innerHTML = buildSectionsHtml(tpl);
        markDirty();
      } else {
        editorEl.value.insertAdjacentHTML('beforeend', buildSectionsHtml(tpl));
        markDirty();
      }
      saveCaret();
      current.template_type = tpl.type;
      await api('/api/reports/template-inserted', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ report_id: current.id, template_type: tpl.type }),
      });
      showToast('已插入「' + tpl.name + '」框架（' + tpl.sections.length + ' 个章节）', 'success');
      scheduleSaveSoon();
    }

    function scheduleSaveSoon() {
      // 模板/引用等结构性变更延迟 3s 落盘，避免刷新丢失
      clearTimeout(debTimer);
      debTimer = setTimeout(saveNow, 3000);
    }

    // ---------- 引用数据 ----------
    const refDlg = reactive({
      show: false, file_id: null, field: '', agg: 'count',
      loading: false, result: null,
    });
    const refFields = ref([]);
    const canCalc = computed(() =>
      refDlg.file_id != null && refDlg.field && !refDlg.loading);

    function openRefDialog() {
      Object.assign(refDlg, { show: true, file_id: null, field: '', agg: 'count', loading: false, result: null });
      refFields.value = [];
      loadFiles();
    }

    async function onRefFileChange() {
      refDlg.field = '';
      refDlg.result = null;
      refFields.value = [];
      if (refDlg.file_id == null) return;
      const body = await api('/api/files/' + refDlg.file_id + '/preview');
      if (body.code !== 0) { showToast(body.msg || '字段列表加载失败', 'error'); return; }
      refFields.value = body.data.fields || [];
    }

    async function doCalc() {
      if (!canCalc.value) return;
      refDlg.loading = true;
      const body = await api('/api/reports/calc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_id: refDlg.file_id, field: refDlg.field, agg: refDlg.agg }),
      });
      refDlg.loading = false;
      if (body.code !== 0) { showToast(body.msg || '计算失败', 'error'); return; }
      const r = body.data;
      refDlg.result = r;

      // 插入引用卡片：contenteditable=false + data-ref 存 JSON；refs_json 同步记录来源
      const refObj = {
        file_id: r.file_id, file_name: r.file_name, field: r.field,
        agg: r.agg, agg_label: r.agg_label, value: r.value, ts: nowHMS(),
      };
      const span = document.createElement('span');
      span.className = 'ref-card';
      span.setAttribute('contenteditable', 'false');
      span.dataset.ref = JSON.stringify(refObj);
      span.textContent = fmtNum(r.value) + '（来源：' + r.file_name + ' · ' + r.field + ' ' + r.agg_label + '）';
      insertNodeAtCursor(span);
      current.refs.push(refObj);
      refDlg.show = false;
      showToast('引用卡片已插入，来源已记录', 'success');
      scheduleSaveSoon();
    }

    // ---------- 导出 Word/PDF（卡10：先核对数据引用清单） ----------
    const expModal = reactive({ show: false, refs: [], format: 'word' });
    const expLoading = ref(false);
    const expCheckedCnt = computed(() => expModal.refs.filter(r => r.checked).length);
    const expAllChecked = computed(() =>
      expModal.refs.length === 0 || expCheckedCnt.value === expModal.refs.length);

    async function beginExport(format) {
      if (!current.id) return;
      updateUnsourced();
      if (unsourcedCnt.value > 0) {
        // 防幻觉硬约束：正文存在无来源数字时前端直接拦截（后端导出接口也有兜底）
        showToast('存在 ' + unsourcedCnt.value + ' 处无数据来源数字（红色标注），禁止导出；请核对或删除后再导出', 'error');
        return;
      }
      await saveNow();  // 先落盘，保证导出的是最新内容
      const body = await api('/api/reports/' + current.id);
      if (body.code !== 0) { showToast(body.msg || '报告加载失败', 'error'); return; }
      expModal.refs = (Array.isArray(body.data.refs) ? body.data.refs : [])
        .map(r => ({ ...r, checked: false }));
      expModal.format = format;
      expModal.show = true;
    }

    function toggleAllRefs() {
      const target = expCheckedCnt.value !== expModal.refs.length;
      expModal.refs.forEach(r => { r.checked = target; });
    }

    function confirmExport(format) {
      if (!expAllChecked.value) { showToast('请逐条核对数据来源', 'error'); return; }
      if (format === 'pdf') {
        // 同步打开打印视图（用户手势内），避免异步后弹窗被浏览器拦截
        window.open('/api/reports/' + current.id + '/print-view', '_blank');
        expModal.show = false;
        showToast('打印视图已打开：点击顶部「打印 / 另存 PDF」完成导出', 'success');
        return;
      }
      downloadWord();
    }

    function fileNameFromDisposition(cd, fallback) {
      if (!cd) return fallback;
      const m1 = cd.match(/filename\*=utf-8''([^;]+)/i);
      if (m1) { try { return decodeURIComponent(m1[1]); } catch (e) {} }
      const m2 = cd.match(/filename="?([^";]+)"?/i);
      return m2 ? m2[1] : fallback;
    }

    async function downloadWord() {
      expLoading.value = true;
      try {
        const res = await fetch('/api/reports/' + current.id + '/export/word', { method: 'POST' });
        const ct = res.headers.get('content-type') || '';
        if (!res.ok || ct.indexOf('application/json') >= 0) {
          let msg = '导出失败，请重启服务后重试';
          try { const j = await res.json(); if (j && j.msg) msg = j.msg; } catch (e) {}
          showToast(msg, 'error');
          return;
        }
        const blob = await res.blob();
        const name = fileNameFromDisposition(
          res.headers.get('content-disposition'), (current.title || '报告') + '.docx');
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = name;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 4000);
        expModal.show = false;
        showToast('Word 已导出：' + name, 'success');
      } catch (e) {
        showToast('导出失败，请确认服务已启动后重试', 'error');
      } finally {
        expLoading.value = false;
      }
    }

    // ---------- AI 构架 / 精炼（卡11，防幻觉硬约束） ----------
    const aiCfg = reactive({ has_key: false, offline_mode: true, ready: false });
    const aiBusy = ref(false);
    const unsourcedCnt = ref(0);
    const aiFields = ref([]);  // {field_name, inferred_type, file_name, masked}

    const aiReady = computed(() => aiCfg.ready);

    function updateUnsourced() {
      unsourcedCnt.value = editorEl.value
        ? editorEl.value.querySelectorAll('span.unsourced').length : 0;
    }

    async function loadAiStatus() {
      const body = await api('/api/settings/ai');
      if (body.code === 0 && body.data) {
        aiCfg.has_key = !!body.data.has_key;
        aiCfg.offline_mode = !!body.data.offline_mode;
        aiCfg.ready = aiCfg.has_key && !aiCfg.offline_mode
          && !!body.data.ai_base_url && !!body.data.ai_model;
      } else {
        aiCfg.ready = false;
      }
    }

    function aiGuard() {
      if (aiCfg.offline_mode) {
        showToast('离线模式已开启，AI 功能不可用；可到「设置」中关闭离线模式', 'error');
        return false;
      }
      if (!aiCfg.has_key) {
        showToast('尚未配置 API Key，请到「设置 → AI 构架/精炼」配置后使用', 'error');
        return false;
      }
      if (!aiCfg.ready) {
        showToast('AI 配置不完整（Base URL / 模型），请到「设置」中补全', 'error');
        return false;
      }
      return true;
    }

    // 字段清单：仅发送字段名；勾选 masked 的字段名在后端拼 prompt 前替换为 [已脱敏字段]
    async function aiAddFields(fileId) {
      if (fileId == null || fileId === '') return;
      const fid = Number(fileId);
      fwModal.file_id = null;
      polModal.file_id = null;
      const body = await api('/api/files/' + fid + '/preview');
      if (body.code !== 0) { showToast(body.msg || '字段列表加载失败', 'error'); return; }
      const f = files.value.find(x => x.id === fid);
      const fname = f ? f.origin_name : ('文件#' + fid);
      let added = 0;
      (body.data.fields || []).forEach(fld => {
        if (!aiFields.value.some(x => x.field_name === fld.field_name)) {
          aiFields.value.push({
            field_name: fld.field_name, inferred_type: fld.inferred_type || '',
            file_name: fname, masked: false,
          });
          added++;
        }
      });
      showToast(added ? ('已添加 ' + added + ' 个字段') : '该文件字段已全部在清单中', 'success');
    }

    function aiFieldPayload() {
      return {
        fields: aiFields.value.map(f => f.field_name),
        masked_fields: aiFields.value.filter(f => f.masked).map(f => f.field_name),
      };
    }

    // 白名单数值来源：只发字段名/聚合方式/数值，绝不含数据行
    function aiRefsPayload() {
      return current.refs.map(r => ({ field: r.field, agg_label: r.agg_label, value: r.value }));
    }

    // ----- AI 构架 -----
    const fwModal = reactive({
      show: false, topic: '', template_type: '', file_id: null,
      loading: false, html: '', unsourced: 0,
    });

    function openAiFramework() {
      if (!aiGuard()) return;
      Object.assign(fwModal, {
        show: true,
        topic: fwModal.topic || current.title || '',
        template_type: current.template_type || '',
        file_id: null, loading: false, html: '', unsourced: 0,
      });
      loadFiles();
    }

    async function doAiFramework() {
      if (!fwModal.topic.trim()) { showToast('请填写报告主题/需求描述', 'error'); return; }
      fwModal.loading = true; fwModal.html = ''; fwModal.unsourced = 0; aiBusy.value = true;
      const body = await api('/api/ai/framework', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          topic: fwModal.topic.trim(),
          template_type: fwModal.template_type || '',
          refs: aiRefsPayload(),
          ...aiFieldPayload(),
        }),
      });
      fwModal.loading = false; aiBusy.value = false;
      if (body.code !== 0) {
        showToast((body.msg || '框架生成失败') + '（原文未改动，可重试）', 'error');
        return;  // 失败保留弹窗与已填内容，可重试
      }
      fwModal.html = body.data.html;
      fwModal.unsourced = body.data.unsourced_numbers_cnt || 0;
      if (fwModal.unsourced > 0) {
        showToast('框架已生成，但有 ' + fwModal.unsourced + ' 处无来源数字（红色），插入后请核对或删除', 'error');
      } else {
        showToast('框架已生成，未发现外造数字', 'success');
      }
    }

    function insertAiFramework() {
      if (!fwModal.html) return;
      insertHtmlAtCursor(fwModal.html);
      if (fwModal.template_type) current.template_type = fwModal.template_type;
      fwModal.show = false;
      updateUnsourced();
      scheduleSaveSoon();
      showToast('AI 框架已插入编辑器', 'success');
    }

    // ----- AI 精炼 -----
    const polModal = reactive({
      show: false, mode: 'professional', scope: 'selection', file_id: null,
      origText: '', loading: false, html: '', unsourced: 0, dropped: [],
    });

    function getEditorSelectionText() {
      const sel = window.getSelection();
      if (sel && sel.rangeCount && editorEl.value &&
          editorEl.value.contains(sel.getRangeAt(0).commonAncestorContainer)) {
        const t = sel.toString();
        if (t && t.trim()) return t;
      }
      if (lastRange && editorEl.value && editorEl.value.contains(lastRange.commonAncestorContainer)) {
        const t = lastRange.toString();
        if (t && t.trim()) return t;
      }
      return '';
    }

    function openAiPolish() {
      if (!aiGuard()) return;
      const selText = getEditorSelectionText();
      const scope = selText ? 'selection' : 'full';
      const text = selText || (editorEl.value ? editorEl.value.innerText : '');
      if (!text || !text.trim()) {
        showToast('请先选中要精炼的段落，或先撰写正文', 'error');
        return;
      }
      saveCaret();  // 保存选区，接受结果时替换选段
      Object.assign(polModal, {
        show: true, mode: polModal.mode || 'professional', scope,
        file_id: null, origText: text, loading: false, html: '', unsourced: 0, dropped: [],
      });
      loadFiles();
    }

    async function doAiPolish() {
      if (!polModal.origText.trim()) return;
      polModal.loading = true; polModal.html = ''; polModal.dropped = []; aiBusy.value = true;
      const body = await api('/api/ai/polish', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: polModal.origText,
          mode: polModal.mode,
          refs: aiRefsPayload(),
          ...aiFieldPayload(),
        }),
      });
      polModal.loading = false; aiBusy.value = false;
      if (body.code !== 0) {
        showToast((body.msg || '精炼失败') + '（原文未改动，可重试）', 'error');
        return;  // 失败安全：原文保留，弹窗不关闭，可重试
      }
      polModal.html = body.data.html;
      polModal.unsourced = body.data.unsourced_numbers_cnt || 0;
      polModal.dropped = body.data.dropped_numbers || [];
      if (polModal.dropped.length) {
        showToast('原文有 ' + polModal.dropped.length + ' 个数字在精炼结果中丢失，建议放弃后重试', 'error');
      } else if (polModal.unsourced > 0) {
        showToast('精炼完成，但有 ' + polModal.unsourced + ' 处新增无来源数字（红色）', 'error');
      } else {
        showToast('精炼完成，原文数字完整保留', 'success');
      }
    }

    function acceptAiPolish() {
      if (!polModal.html) return;
      if (polModal.scope === 'full') {
        editorEl.value.innerHTML = polModal.html;  // 全文模式：整体替换（弹窗已警示卡片/图片转文本）
      } else {
        insertHtmlAtCursor(polModal.html);         // 选段模式：替换原选区
      }
      polModal.show = false;
      markDirty();
      updateUnsourced();
      scheduleSaveSoon();
      showToast('精炼结果已应用到编辑器', 'success');
    }

    // ---------- 片段库联动（liteops:insert-snippet） ----------
    function insertSnippetBlock(sql, title) {
      const pre = document.createElement('pre');
      pre.className = 'snippet-block';
      pre.textContent = title ? '-- ' + title + '\n' + sql : sql;
      insertNodeAtCursor(pre);
    }

    function consumePendingSnippet() {
      try {
        const raw = sessionStorage.getItem(PENDING_SNIPPET_KEY);
        if (!raw) return null;
        sessionStorage.removeItem(PENDING_SNIPPET_KEY);
        return JSON.parse(raw);
      } catch (e) { return null; }
    }

    function peekPendingSnippet() {
      try {
        const raw = sessionStorage.getItem(PENDING_SNIPPET_KEY);
        return raw ? JSON.parse(raw) : null;
      } catch (e) { return null; }
    }

    function onInsertSnippet(e) {
      const sql = e.detail && e.detail.sql;
      if (!sql || mode.value !== 'editor' || !editorEl.value) return;
      sessionStorage.removeItem(PENDING_SNIPPET_KEY);
      insertSnippetBlock(sql, e.detail.title);
      showToast('SQL 片段已插入到报告', 'success');
      scheduleSaveSoon();
    }

    // ---------- 打开 / 返回 / 删除 ----------
    async function openReport(item) {
      const body = await api('/api/reports/' + item.id);
      if (body.code !== 0) { showToast(body.msg || '报告加载失败', 'error'); return; }
      const d = body.data;
      current.id = d.id;
      current.title = d.title || '';
      current.ticket_id = d.ticket_id;
      current.template_type = d.template_type || '';
      current.status = d.status || '草稿';
      current.refs = Array.isArray(d.refs) ? d.refs : [];
      mode.value = 'editor';
      await nextTick();
      editorEl.value.innerHTML = d.content_html || '';
      dirty.value = false;
      lastSaveTs = 0;
      lastRange = null;
      saveState.value = 'saved';
      savedAtText.value = (d.updated_at || '').slice(11, 19) || '—';

      // 片段库暂存的 SQL：打开编辑器后自动插入
      const pending = consumePendingSnippet();
      if (pending && pending.sql) {
        insertSnippetBlock(pending.sql, pending.title);
        showToast('SQL 片段已插入到报告', 'success');
      }
      // 新建时选了模板且正文为空 → 自动插入框架章节
      if (!d.content_html && current.template_type) {
        const tpl = templates.value.find(t => t.type === current.template_type);
        if (tpl) {
          editorEl.value.innerHTML = buildSectionsHtml(tpl);
          markDirty();
          scheduleSaveSoon();
          showToast('已插入「' + tpl.name + '」框架（' + tpl.sections.length + ' 个章节）', 'success');
        }
      }
    }

    function backToList() {
      flushSave();
      mode.value = 'list';
      loadList();
    }

    async function removeReport(r) {
      if (!window.confirm('确定删除报告《' + r.title + '》？此操作不可恢复')) return;
      const body = await api('/api/reports/' + r.id, { method: 'DELETE' });
      if (body.code !== 0) { showToast(body.msg || '删除失败', 'error'); return; }
      showToast('报告已删除', 'success');
      loadList();
    }

    // ---------- 生命周期 ----------
    onMounted(() => {
      // execCommand 风格确定性设置：加粗/斜体用 <b> 标签而非 span 内联样式，段落默认 p
      document.execCommand('styleWithCSS', false, 'false');
      document.execCommand('defaultParagraphSeparator', false, 'p');
      window.addEventListener('liteops:insert-snippet', onInsertSnippet);
      loadList();
      loadTemplates();
      loadTickets();
      loadAiStatus();  // 卡11：AI 入口可用状态（离线/未配 Key 时按钮禁用）
      if (peekPendingSnippet()) {
        showToast('SQL 片段已暂存，打开一份报告后自动插入', 'info');
      }
    });
    onUnmounted(() => {
      window.removeEventListener('liteops:insert-snippet', onInsertSnippet);
      clearTimeout(debTimer);
      flushSave();
    });

    return {
      toast, mode, list, tickets, templates, files, loading, creating, aggOptions,
      createModal, openCreate, submitCreate, ticketTitle, openReport, backToList, removeReport,
      current, editorEl, saveState, savedAtText, onInput, onKeydown, saveNow,
      fmtNum, execCmd, fmtBlock, insertTable, onPaste,
      tplModal, openTplModal, confirmInsertTemplate,
      refDlg, refFields, canCalc, openRefDialog, onRefFileChange, doCalc,
      expModal, expLoading, expCheckedCnt, expAllChecked,
      beginExport, toggleAllRefs, confirmExport,
      // 卡11：AI 构架/精炼 + 防幻觉
      aiReady, aiBusy, unsourcedCnt, aiFields,
      fwModal, openAiFramework, doAiFramework, insertAiFramework, aiAddFields,
      polModal, openAiPolish, doAiPolish, acceptAiPolish,
    };
  },
};
})();
