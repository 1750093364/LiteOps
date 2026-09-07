/* 需求清单视图（卡08）。
   需求看板：按「紧急性 × 截止紧迫度 × 业务方」打分降序，支持 HTML5 拖拽手动覆盖排序；
   新建/编辑弹窗（全字段）、状态机按钮组（返工必填意见）、实际工时回填；
   每条可打开「分析框架确认页」独立打印视图（应用内全屏页：五分区 + 签字行 + 打印/另存 PDF，
   @media print 隐藏操作区；底部业务方确认 / 退回修改按钮回写状态）。
   支持 ?framework={id} 深链：业务方打开链接直达确认页。
   Vue3 全局构建，无构建步骤。整体包在 IIFE 内，避免全局 const 冲突。 */
(function () {
const { ref, reactive, computed, onMounted } = Vue;

const STATUS_LABELS = {
  pending: '待确认', confirmed: '已确认', scheduled: '排期中',
  in_progress: '进行中', delivered: '已交付', rework: '返工中', canceled: '已取消',
};
const STATUS_ORDER = ['pending', 'confirmed', 'scheduled', 'in_progress', 'rework', 'delivered', 'canceled'];
const URGENCIES = ['高', '中', '低'];
const ANALYSIS_TYPES = ['活动复盘', '漏斗/路径', '留存流失', '渠道效果', '异常分析', 'A/B 测试', '专题分析'];

// 状态机：各状态卡片上展示的下一步动作（与后端 ticket_service.STATUS_FLOW 一致）
const NEXT_ACTIONS = {
  pending: [
    { to: 'confirmed', label: '确认通过', kind: 'primary' },
    { to: 'rework', label: '退回修改', kind: 'danger' },
    { to: 'canceled', label: '取消需求', kind: '' },
  ],
  confirmed: [
    { to: 'scheduled', label: '排入排期', kind: '' },
    { to: 'in_progress', label: '开始开工', kind: 'primary' },
    { to: 'canceled', label: '取消需求', kind: 'danger' },
  ],
  scheduled: [
    { to: 'in_progress', label: '开始工作', kind: 'primary' },
    { to: 'canceled', label: '取消需求', kind: 'danger' },
  ],
  in_progress: [
    { to: 'delivered', label: '完成交付', kind: 'primary' },
    { to: 'rework', label: '退回返工', kind: 'danger' },
  ],
  rework: [
    { to: 'in_progress', label: '返工完成', kind: 'primary' },
    { to: 'canceled', label: '取消需求', kind: 'danger' },
  ],
  delivered: [
    { to: 'rework', label: '退回返工', kind: 'danger' },
  ],
  canceled: [],
};

// 框架确认页五分区
const FW_FIELDS = [
  { key: 'objective', label: '一、分析目标', rows: 3 },
  { key: 'caliber', label: '二、指标口径', rows: 4 },
  { key: 'dimensions', label: '三、分析维度', rows: 3 },
  { key: 'daterange', label: '四、时间范围', rows: 2 },
  { key: 'deliverable', label: '五、交付物与时间', rows: 3 },
];
const FW_HIST_LABEL = { confirm: '确认通过', rework: '退回修改', note: '补充意见' };

window.TicketsView = {
  name: 'TicketsView',
  template: `
  <div class="tk-view">
    <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.msg }}</div>

    <!-- ========== 看板 ========== -->
    <div class="view-head">
      <div>
        <h1>需求清单</h1>
        <p>业务方数据需求统一录入：框架先行、确认后开工；按得分排序，先做哪个一目了然</p>
      </div>
      <div class="flex-row">
        <button class="btn" v-if="manual" @click="resetAuto">↺ 恢复自动排序</button>
        <button class="btn btn-primary" @click="openCreate">＋ 新建需求</button>
      </div>
    </div>

    <div class="tk-rulebar">
      <span>
        得分 = 紧急性(高3/中2/低1) × <b>{{ weights.w_urgency }}</b>
        ＋ 截止(逾期4/24h内3/3天内2/其他1) × <b>{{ weights.w_deadline }}</b>
        ＋ 业务方(1.5) × <b>{{ weights.w_requester }}</b>
      </span>
      <span class="tk-rule-manual" v-if="manual">🖐 手动拖拽排序中</span>
      <a href="javascript:void(0)" class="tk-rule-link" @click="gotoSettings">去设置调权重 →</a>
    </div>

    <div class="tk-filters">
      <span class="tk-chip" :class="{ active: filter === '' }" @click="filter = ''">
        全部 {{ counts.all || 0 }}
      </span>
      <span v-for="s in statusOrder" :key="s" class="tk-chip"
            :class="{ active: filter === s }"
            @click="filter = s">
        {{ STATUS_LABELS[s] }} {{ counts[s] || 0 }}
      </span>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="filteredList.length === 0" class="empty">
      {{ filter ? '该状态下暂无需求' : '暂无需求，点击「新建需求」录入第一条' }}
    </div>
    <div v-else class="tk-list">
      <div v-for="t in filteredList" :key="t.id" class="tk-card"
           :class="{ dragging: dragId === t.id }"
           draggable="true"
           @dragstart="onDragStart($event, t.id)"
           @dragover="onDragOver($event, t.id)"
           @dragleave="onDragLeave"
           @drop="onDrop($event, t.id)"
           @dragend="onDragEnd">
        <div class="tk-rank">{{ rankOf(t) }}</div>
        <div class="tk-drag" title="拖拽调整优先级">⋮⋮</div>
        <div class="tk-main">
          <div class="tk-title-line">
            <span class="tk-title">{{ t.title }}</span>
            <span class="badge tk-urg" :class="'urg-' + urgencyKey(t.urgency)">{{ t.urgency }}紧急</span>
            <span class="badge tk-status" :class="'st-' + t.status">{{ t.status_label }}</span>
            <span class="badge tk-fw" v-if="t.framework_confirmed">📝 框架已确认</span>
          </div>
          <div class="tk-sub">
            <span>👤 {{ t.requester || '未填需求方' }}</span>
            <span v-if="t.analysis_type">📊 {{ t.analysis_type }}</span>
            <span v-if="t.est_hours != null">⏱ 预计 {{ t.est_hours }}h</span>
            <span v-if="t.actual_hours != null">✅ 实际 {{ t.actual_hours }}h</span>
            <span>🕐 {{ t.created_at }}</span>
          </div>
          <div class="tk-desc" v-if="t.description">{{ t.description }}</div>
          <div class="tk-note-line" v-if="t.latest_note">📝 最近意见：{{ t.latest_note }}</div>
          <div class="tk-ops">
            <button class="btn btn-mini btn-primary" @click="openFramework(t)">框架确认页</button>
            <button class="btn btn-mini" @click="openEdit(t)">编辑</button>
            <button v-for="a in nextActions(t)" :key="a.to"
                    class="btn btn-mini"
                    :class="a.kind === 'primary' ? 'btn-primary' : (a.kind === 'danger' ? 'btn-danger' : '')"
                    @click="transit(t, a)">{{ a.label }}</button>
          </div>
        </div>
        <div class="tk-side">
          <div class="tk-deadline" :class="deadlineInfo(t).cls">{{ deadlineInfo(t).text }}</div>
          <div class="tk-score" :title="scoreTip(t)">
            <b>{{ Number(t.score).toFixed(2) }}</b><span>优先级得分</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ========== 新建 / 编辑需求弹窗 ========== -->
    <div class="modal-mask" v-if="modal.show" @click.self="modal.show = false">
      <div class="modal tk-modal">
        <h3>{{ modal.id ? '编辑需求' : '新建需求' }}</h3>
        <label class="fm-label">需求标题 *
          <input v-model="modal.title" placeholder="如：大促期间各渠道转化漏斗复盘">
        </label>
        <div class="flex-row">
          <label class="fm-label" style="flex:1">需求方
            <input v-model="modal.requester" placeholder="如：市场部 王敏">
          </label>
          <label class="fm-label" style="flex:1">紧急性
            <select v-model="modal.urgency">
              <option v-for="u in urgencies" :key="u" :value="u">{{ u }}紧急</option>
            </select>
          </label>
        </div>
        <div class="flex-row">
          <label class="fm-label" style="flex:1">期望截止时间
            <input type="date" v-model="modal.deadline">
          </label>
          <label class="fm-label" style="flex:1">关联分析类型
            <select v-model="modal.analysis_type">
              <option value="">（未选）</option>
              <option v-for="a in analysisTypes" :key="a" :value="a">{{ a }}</option>
            </select>
          </label>
        </div>
        <div class="flex-row">
          <label class="fm-label" style="flex:1">预计工时（小时）
            <input type="number" min="0" step="0.5" v-model="modal.est_hours" placeholder="如 8">
          </label>
          <label class="fm-label" style="flex:1" v-if="modal.id && modal.status !== 'pending'">实际工时（小时）
            <input type="number" min="0" step="0.5" v-model="modal.actual_hours" placeholder="交付后回填">
          </label>
        </div>
        <label class="fm-label">需求描述
          <textarea rows="3" v-model="modal.description"
                    placeholder="业务背景、想解决的问题、期望产出…"></textarea>
        </label>
        <div class="modal-foot">
          <button class="btn" @click="modal.show = false">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="saveTicket">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ========== 返工 / 退回意见弹窗（看板卡片用） ========== -->
    <div class="modal-mask" v-if="noteModal.show" @click.self="noteModal.show = false">
      <div class="modal">
        <h3>退回修改 / 返工意见</h3>
        <p style="font-size:13px;color:var(--text-sub);margin-bottom:10px">
          退回必须填写原因，意见将随框架确认页留痕，业务方可见。
        </p>
        <label class="fm-label">退回意见 *
          <textarea rows="4" v-model="noteModal.note"
                    placeholder="如：转化口径请按支付成功口径调整，时间范围需对齐大促周期…"></textarea>
        </label>
        <div class="modal-foot">
          <button class="btn" @click="noteModal.show = false">取消</button>
          <button class="btn btn-danger" :disabled="saving" @click="confirmRework">确认退回</button>
        </div>
      </div>
    </div>

    <!-- ========== 框架确认页（独立打印视图，全屏覆盖） ========== -->
    <div class="fw-overlay" v-if="fw.show">
      <div class="fw-toolbar no-print">
        <button class="fw-btn" @click="fwClose">← 返回看板</button>
        <button class="fw-btn fw-primary" @click="fwPrint">🖨 打印 / 另存 PDF</button>
        <button class="fw-btn" @click="fwCopyLink">🔗 复制链接</button>
        <span style="flex:1"></span>
        <button class="fw-btn" v-if="!fw.editing" @click="fwStartEdit">✎ 编辑框架</button>
        <button class="fw-btn fw-primary" v-if="fw.canConfirm" @click="fwConfirm">✅ 业务方已确认</button>
        <button class="fw-btn fw-danger" v-if="fw.canRework" @click="fw.reworkBox = !fw.reworkBox">↩ 退回修改</button>
      </div>

      <div class="fw-banner" v-if="fw.banner" :class="fw.banner.ok ? 'ok' : 'err'">{{ fw.banner.msg }}</div>

      <div class="fw-rework no-print" v-if="fw.canRework && fw.reworkBox">
        <div class="fw-rework-title">请填写退回意见（必填，将随确认页留痕）</div>
        <textarea rows="3" v-model="fw.reworkNote"
                  placeholder="如：转化口径请按支付成功口径调整，时间范围需对齐大促周期…"></textarea>
        <div style="text-align:right;margin-top:8px;">
          <button class="fw-btn" @click="fw.reworkBox = false">取消</button>
          <button class="fw-btn fw-danger" :disabled="fw.busy" @click="fwReworkSubmit">确认退回</button>
        </div>
      </div>

      <div class="fw-page" v-if="fw.data">
        <div class="fw-brand">轻析 LiteOps · 分析框架确认页</div>
        <div class="fw-title">{{ fw.data.ticket.title }}</div>
        <div class="fw-meta">
          <span>需求方：<b>{{ fw.data.ticket.requester || '未填写' }}</b></span>
          <span>紧急性：<b>{{ fw.data.ticket.urgency || '中' }}</b></span>
          <span v-if="fw.data.ticket.deadline">期望截止：<b>{{ fw.data.ticket.deadline }}</b></span>
          <span v-if="fw.data.ticket.analysis_type">分析类型：<b>{{ fw.data.ticket.analysis_type }}</b></span>
          <span v-if="fw.data.ticket.est_hours != null">预计工时：<b>{{ fw.data.ticket.est_hours }}h</b></span>
          <span>当前状态：<b>{{ fw.data.ticket.status_label }}</b></span>
        </div>

        <!-- 只读视图 -->
        <template v-if="!fw.editing">
          <div class="fw-sec" v-for="f in fwFields" :key="f.key">
            <div class="fw-sec-h">{{ f.label }}</div>
            <div class="fw-sec-b">
              <span v-if="fw.data.framework[f.key] && fw.data.framework[f.key].trim()">{{ fw.data.framework[f.key] }}</span>
              <span v-else class="fw-empty">（待填写）</span>
            </div>
          </div>
          <div class="fw-sec" v-if="fw.data.note">
            <div class="fw-sec-h">最近确认 / 退回意见</div>
            <div class="fw-sec-b">{{ fw.data.note }}</div>
          </div>
        </template>

        <!-- 编辑视图 -->
        <template v-else>
          <div class="fw-edit-sec" v-for="f in fwFields" :key="f.key">
            <div class="fw-sec-h">{{ f.label }}</div>
            <textarea :rows="f.rows" v-model="fwForm[f.key]"
                      :placeholder="'请填写' + f.label.slice(2) + '…'"></textarea>
          </div>
          <div class="fw-edit-foot no-print">
            <button class="fw-btn" @click="fw.editing = false">取消编辑</button>
            <button class="fw-btn fw-primary" :disabled="fw.busy" @click="fwSave">💾 保存框架</button>
          </div>
        </template>

        <div class="fw-sign">
          <div class="fw-sign-item"><span>业务方签字</span><div class="line"></div></div>
          <div class="fw-sign-item"><span>数据运营签字</span><div class="line"></div></div>
          <div class="fw-sign-item fw-sign-date"><span>日期</span><div class="line"></div></div>
        </div>
      </div>

      <div class="fw-hist" v-if="fw.data && fw.data.history && fw.data.history.length">
        <div class="fw-hist-title">确认 / 退回记录</div>
        <div class="fw-hist-item" v-for="(h, idx) in fwHistoryDesc" :key="idx">
          <span :class="'act-' + h.action">{{ fwHistLabel[h.action] || h.action }}</span>
          · {{ h.at }}<template v-if="h.note">：{{ h.note }}</template>
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
      try {
        res = await fetch(path, opts);
      } catch (e) {
        return { code: -1, msg: '网络请求失败，请确认服务已启动' };
      }
      // HTTP 层错误（404/405/500 等）：多为后端版本过旧或服务异常，给出可操作提示
      if (!res.ok) {
        let msg = '服务接口异常（HTTP ' + res.status + '），请重启服务后重试';
        try {
          const j = await res.json();
          if (j && j.msg) msg = j.msg + '（HTTP ' + res.status + '；若刚更新过版本，请重启服务）';
        } catch (_) { /* 非 JSON 响应，沿用默认提示 */ }
        return { code: res.status || -1, msg };
      }
      try { return await res.json(); }
      catch { return { code: -1, msg: '服务响应异常' }; }
    }

    // ---------- 看板数据 ----------
    const list = ref([]);          // /ranked 看板顺序（不含已取消）
    const allList = ref([]);       // /api/tickets 全量（状态计数用）
    const weights = reactive({ w_urgency: 0.5, w_deadline: 0.3, w_requester: 0.2 });
    const manual = ref(false);
    const filter = ref('');
    const loading = ref(false);
    const saving = ref(false);
    const dragId = ref(null);
    const dragOverId = ref(null);
    const statusOrder = STATUS_ORDER;
    const urgencies = URGENCIES;
    const analysisTypes = ANALYSIS_TYPES;
    const fwFields = FW_FIELDS;
    const fwHistLabel = FW_HIST_LABEL;

    const counts = computed(() => {
      const c = { all: allList.value.length };
      for (const s of STATUS_ORDER) c[s] = 0;
      for (const t of allList.value) c[t.status] = (c[t.status] || 0) + 1;
      return c;
    });

    const filteredList = computed(() =>
      filter.value ? list.value.filter(t => t.status === filter.value) : list.value
    );

    async function load() {
      loading.value = true;
      const [rRank, rAll] = await Promise.all([
        api('/api/tickets/ranked'),
        api('/api/tickets'),
      ]);
      loading.value = false;
      if (rRank.code !== 0) { showToast(rRank.msg || '看板加载失败', 'error'); return; }
      list.value = rRank.data.list;
      Object.assign(weights, rRank.data.weights);
      manual.value = !!rRank.data.manual;
      if (rAll.code === 0) allList.value = rAll.data.list;
    }

    function rankOf(t) {
      const i = list.value.findIndex(x => x.id === t.id);
      return i >= 0 ? i + 1 : '-';
    }

    function nextActions(t) { return NEXT_ACTIONS[t.status] || []; }
    function urgencyKey(u) { return ({ '高': 'high', '中': 'mid', '低': 'low' })[u] || 'mid'; }

    // ---------- 截止时间提示 ----------
    function parseDeadline(s) {
      if (!s) return null;
      const d = new Date(s.length === 10 ? s + 'T23:59:59' : s);
      return isNaN(d.getTime()) ? null : d;
    }
    function fmtMd(d) { return (d.getMonth() + 1) + '月' + d.getDate() + '日'; }
    function deadlineInfo(t) {
      if (!t.deadline) return { text: '未设截止', cls: 'tk-dl-none' };
      const d = parseDeadline(t.deadline);
      if (!d) return { text: t.deadline, cls: 'tk-dl-none' };
      if (t.status === 'delivered' || t.status === 'canceled') {
        return { text: '截止 ' + fmtMd(d), cls: 'tk-dl-done' };
      }
      const diffH = (d.getTime() - Date.now()) / 3.6e6;
      if (diffH < 0) return { text: '⛔ 已逾期 ' + Math.ceil(-diffH / 24) + ' 天', cls: 'tk-dl-over' };
      if (diffH <= 24) return { text: '⏰ 24h 内截止', cls: 'tk-dl-soon' };
      if (diffH <= 72) return { text: '⏰ ' + Math.ceil(diffH / 24) + ' 天内截止', cls: 'tk-dl-soon' };
      return { text: fmtMd(d) + ' 截止', cls: 'tk-dl-normal' };
    }

    function scoreTip(t) {
      const w = weights;
      return '得分构成：\n'
        + '紧急性 ' + t.u_score + ' × ' + w.w_urgency + ' = ' + (t.u_score * w.w_urgency).toFixed(2) + '\n'
        + '截止紧迫度 ' + t.d_score + ' × ' + w.w_deadline + ' = ' + (t.d_score * w.w_deadline).toFixed(2) + '\n'
        + '业务方 ' + t.r_score + ' × ' + w.w_requester + ' = ' + (t.r_score * w.w_requester).toFixed(2);
    }

    // ---------- 拖拽排序（前端产生有序 id 列表，后端落 sort_order） ----------
    function onDragStart(e, id) {
      dragId.value = id;
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', String(id)); } catch (_) {}
    }
    function onDragOver(e, id) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      dragOverId.value = id;
    }
    function onDragLeave() {
      if (dragOverId.value) dragOverId.value = null;
    }
    async function onDrop(e, targetId) {
      e.preventDefault();
      const srcId = dragId.value;
      dragId.value = null;
      dragOverId.value = null;
      if (!srcId || srcId === targetId) return;
      const arr = list.value.slice();
      const from = arr.findIndex(x => x.id === srcId);
      if (from < 0) return;
      const [moved] = arr.splice(from, 1);
      let to = arr.findIndex(x => x.id === targetId);
      if (to < 0) to = arr.length;
      arr.splice(to, 0, moved);
      list.value = arr;  // 先乐观更新
      const body = await api('/api/tickets/sort-order', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: arr.map(t => t.id) }),
      });
      if (body.code !== 0) {
        showToast(body.msg || '排序保存失败', 'error');
        load();
        return;
      }
      manual.value = true;
      showToast('顺序已保存，刷新页面后保持', 'success');
    }
    function onDragEnd() {
      dragId.value = null;
      dragOverId.value = null;
    }
    async function resetAuto() {
      const body = await api('/api/tickets/sort-order', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [] }),
      });
      if (body.code !== 0) { showToast(body.msg || '重置失败', 'error'); return; }
      showToast('已恢复按得分自动排序', 'success');
      load();
    }

    // ---------- 新建 / 编辑 ----------
    const modal = reactive({
      show: false, id: null, status: 'pending',
      title: '', requester: '', description: '', deadline: '',
      urgency: '中', est_hours: null, analysis_type: '', actual_hours: null,
    });
    function openCreate() {
      Object.assign(modal, {
        show: true, id: null, status: 'pending', title: '', requester: '',
        description: '', deadline: '', urgency: '中', est_hours: null,
        analysis_type: '', actual_hours: null,
      });
    }
    function openEdit(t) {
      Object.assign(modal, {
        show: true, id: t.id, status: t.status, title: t.title,
        requester: t.requester || '', description: t.description || '',
        deadline: t.deadline || '', urgency: t.urgency || '中',
        est_hours: t.est_hours != null ? t.est_hours : null,
        analysis_type: t.analysis_type || '',
        actual_hours: t.actual_hours != null ? t.actual_hours : null,
      });
    }
    function numOrNull(v) {
      if (v === null || v === undefined || v === '') return null;
      const n = Number(v);
      return isNaN(n) ? null : n;
    }
    async function saveTicket() {
      if (!modal.title.trim()) { showToast('请填写需求标题', 'error'); return; }
      const payload = {
        title: modal.title.trim(),
        requester: modal.requester.trim(),
        description: modal.description.trim(),
        deadline: modal.deadline || null,
        urgency: modal.urgency,
        est_hours: numOrNull(modal.est_hours),
        analysis_type: modal.analysis_type,
      };
      let path = '/api/tickets', method = 'POST';
      if (modal.id) {
        path = '/api/tickets/' + modal.id;
        method = 'PUT';
        payload.actual_hours = numOrNull(modal.actual_hours);
      }
      saving.value = true;
      const body = await api(path, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      saving.value = false;
      if (body.code !== 0) { showToast(body.msg || '保存失败', 'error'); return; }
      modal.show = false;
      showToast(modal.id ? '需求已更新' : '需求已录入，状态：待确认', 'success');
      load();
    }

    // ---------- 状态流转 ----------
    const noteModal = reactive({ show: false, ticketId: null, note: '' });
    function transit(t, a) {
      if (a.to === 'rework') {
        noteModal.show = true;
        noteModal.ticketId = t.id;
        noteModal.note = '';
        return;
      }
      doTransit(t.id, { status: a.to });
    }
    function confirmRework() {
      if (!noteModal.note.trim()) { showToast('请填写退回意见', 'error'); return; }
      const id = noteModal.ticketId;
      const note = noteModal.note.trim();
      noteModal.show = false;
      doTransit(id, { status: 'rework', confirm_note: note });
    }
    async function doTransit(id, payload) {
      saving.value = true;
      const body = await api('/api/tickets/' + id + '/status', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      saving.value = false;
      if (body.code !== 0) { showToast(body.msg || '状态更新失败', 'error'); return; }
      showToast('状态已更新为「' + body.data.status_label + '」', 'success');
      load();
      return body.data;
    }

    function gotoSettings() {
      window.dispatchEvent(new CustomEvent('liteops:goto', { detail: 'settings' }));
    }

    // ---------- 框架确认页（应用内全屏打印视图） ----------
    const fw = reactive({
      show: false, data: null, editing: false, busy: false,
      reworkBox: false, reworkNote: '', banner: null,
    });
    const fwForm = reactive({ objective: '', caliber: '', dimensions: '', daterange: '', deliverable: '' });
    const fwHistoryDesc = computed(() =>
      fw.data && fw.data.history ? fw.data.history.slice().reverse() : []
    );
    function fwAllowed(target) {
      const acts = NEXT_ACTIONS[fw.data ? fw.data.ticket.status : ''] || [];
      return acts.some(a => a.to === target);
    }
    Object.defineProperties(fw, {
      canConfirm: { get() { return fwAllowed('confirmed'); } },
      canRework: { get() { return fwAllowed('rework'); } },
    });

    async function openFramework(t) {
      const body = await api('/api/tickets/' + t.id + '/framework');
      if (body.code !== 0) { showToast(body.msg || '框架页加载失败', 'error'); return; }
      fw.data = body.data;
      fw.editing = FW_FIELDS.every(f => !(body.data.framework[f.key] || '').trim());  // 全空→起草态
      fw.banner = null;
      fw.reworkBox = false;
      fw.reworkNote = '';
      fw.show = true;
      document.body.classList.add('fw-open');
      Object.assign(fwForm, body.data.framework);
      window.scrollTo(0, 0);
    }
    function fwClose() {
      fw.show = false;
      fw.data = null;
      document.body.classList.remove('fw-open');
      load();  // 关闭时刷新看板（确认/退回可能已回写状态）
    }
    function fwPrint() { window.print(); }
    function fwStartEdit() {
      Object.assign(fwForm, fw.data.framework);
      fw.editing = true;
      fw.banner = null;
    }
    function fwSetBanner(msg, ok) { fw.banner = { msg, ok }; }
    async function fwSave() {
      fw.busy = true;
      const body = await api('/api/tickets/' + fw.data.ticket.id + '/framework', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...fwForm }),
      });
      fw.busy = false;
      if (body.code !== 0) { fwSetBanner(body.msg || '保存失败', false); return; }
      fw.data.framework = body.data.framework;
      fw.data.note = body.data.note;
      fw.editing = false;
      fwSetBanner('框架已保存，可打印或发业务方确认', true);
      load();
    }
    async function fwConfirm() {
      if (!window.confirm('确认业务方已认可本框架？确认后需求单进入「已确认」状态。')) return;
      fw.busy = true;
      const body = await api('/api/tickets/' + fw.data.ticket.id + '/status', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'confirmed' }),
      });
      fw.busy = false;
      if (body.code !== 0) { fwSetBanner(body.msg || '操作失败', false); return; }
      fw.data.ticket.status = body.data.status;
      fw.data.ticket.status_label = body.data.status_label;
      fw.data.history = body.data.framework_history;
      fwSetBanner('✅ 业务方已确认，需求单状态：已确认。可打印本页存档。', true);
      load();
    }
    function fwReworkSubmit() {
      const note = fw.reworkNote.trim();
      if (!note) { fwSetBanner('退回必须填写意见', false); return; }
      doRework(note);
    }
    async function doRework(note) {
      fw.busy = true;
      const body = await api('/api/tickets/' + fw.data.ticket.id + '/status', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'rework', confirm_note: note }),
      });
      fw.busy = false;
      if (body.code !== 0) { fwSetBanner(body.msg || '操作失败', false); return; }
      fw.data.ticket.status = body.data.status;
      fw.data.ticket.status_label = body.data.status_label;
      fw.data.note = body.data.latest_note || note;
      fw.data.history = body.data.framework_history;
      fw.reworkBox = false;
      fw.reworkNote = '';
      fwSetBanner('↩ 已退回修改，需求单状态：返工中。意见已留痕。', true);
      load();
    }
    async function fwCopyLink() {
      const url = window.location.origin + window.location.pathname + '?framework=' + fw.data.ticket.id;
      let ok = false;
      try {
        await navigator.clipboard.writeText(url);
        ok = true;
      } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = url;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
        document.body.removeChild(ta);
      }
      fwSetBanner(ok ? '链接已复制：' + url + '（业务方打开即见本确认页）' : '复制失败，请手动复制地址栏链接', ok);
    }

    // ---------- 深链：?framework={id} 直达确认页 ----------
    onMounted(() => {
      load();
      const fwId = new URLSearchParams(window.location.search).get('framework');
      if (fwId && /^\d+$/.test(fwId)) {
        setTimeout(() => openFramework({ id: Number(fwId) }), 500);
      }
    });

    return {
      toast, list, weights, manual, filter, loading, saving,
      dragId, counts, filteredList, statusOrder, urgencies, analysisTypes,
      modal, noteModal, STATUS_LABELS,
      fw, fwForm, fwFields, fwHistLabel, fwHistoryDesc,
      rankOf, nextActions, urgencyKey, deadlineInfo, scoreTip,
      onDragStart, onDragOver, onDragLeave, onDrop, onDragEnd, resetAuto,
      openCreate, openEdit, saveTicket,
      transit, confirmRework, gotoSettings,
      openFramework, fwClose, fwPrint, fwCopyLink, fwStartEdit,
      fwSave, fwConfirm, fwReworkSubmit,
    };
  },
};
})();
