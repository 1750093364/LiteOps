/* 设置视图（卡08）。
   需求排序权重调整：紧急性 / 截止紧迫度 / 业务方三项权重（0~1），
   保存后需求清单按新权重实时重排（打分在服务端实时计算）。
   Vue3 全局构建，无构建步骤。整体包在 IIFE 内，避免全局 const 冲突。 */
(function () {
const { ref, reactive, watch, onMounted } = Vue;

window.SettingsView = {
  name: 'SettingsView',
  template: `
  <div class="set-view">
    <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.msg }}</div>

    <div class="view-head">
      <div>
        <h1>设置</h1>
        <p>全局参数配置，保存在本地数据库</p>
      </div>
    </div>

    <div class="set-card">
      <h3 class="set-h3">需求清单排序权重</h3>
      <p class="set-desc">
        需求得分 = 紧急性分 × 紧急性权重 ＋ 截止紧迫度分 × 截止权重 ＋ 业务方分 × 业务方权重，
        得分越高越优先。权重越大，该因素对排序影响越大（取值 0~1，可为 0 表示不考虑）。
      </p>

      <div class="set-factor">
        <div class="set-factor-head">
          <span class="set-factor-name">🔴 紧急性权重</span>
          <input type="number" class="set-weight-input" min="0" max="1" step="0.05"
                 v-model.number="form.w_urgency">
        </div>
        <div class="set-factor-rule">因子分：高紧急 = 3 分，中紧急 = 2 分，低紧急 = 1 分</div>
      </div>

      <div class="set-factor">
        <div class="set-factor-head">
          <span class="set-factor-name">⏰ 截止紧迫度权重</span>
          <input type="number" class="set-weight-input" min="0" max="1" step="0.05"
                 v-model.number="form.w_deadline">
        </div>
        <div class="set-factor-rule">因子分：已逾期 = 4 分，24 小时内截止 = 3 分，3 天内截止 = 2 分，其他 = 1 分</div>
      </div>

      <div class="set-factor">
        <div class="set-factor-head">
          <span class="set-factor-name">👤 业务方权重</span>
          <input type="number" class="set-weight-input" min="0" max="1" step="0.05"
                 v-model.number="form.w_requester">
        </div>
        <div class="set-factor-rule">因子分：业务方分固定 1.5 分（V1.0 不区分需求方）</div>
      </div>

      <div class="set-preview" v-if="previewOk">
        预览：高紧急 + 已逾期需求得分 ≈
        <b>{{ preview.highOver.toFixed(2) }}</b>；低紧急 + 无截止需求得分 ≈
        <b>{{ preview.lowFar.toFixed(2) }}</b>
      </div>

      <div class="set-actions">
        <button class="btn" @click="resetDefaults" :disabled="saving">恢复默认 (0.5 / 0.3 / 0.2)</button>
        <button class="btn btn-primary" @click="save" :disabled="saving">
          {{ saving ? '保存中…' : '保存并重排' }}
        </button>
      </div>
    </div>

    <!-- ========== AI 构架 / 精炼配置（卡11） ========== -->
    <div class="set-card">
      <h3 class="set-h3">AI 构架 / 精炼（自带 Key）</h3>
      <p class="set-desc">
        AI 仅用于报告「构架」（生成章节框架）与「精炼」（优化语言），兼容任意 OpenAI 协议服务
        （OpenAI / DeepSeek / 通义千问等）。
        <b>Key 仅存于本地 SQLite 数据库</b>，不随任何遥测上传；发送给 AI 的内容只包含字段名、
        已引用的聚合数值与你写的文字，<b>绝不包含原始数据行</b>；可对敏感字段勾选脱敏，
        也可开启离线模式完全关闭 AI（其余功能不受影响）。
      </p>

      <div v-if="ai.ai_config_locked" class="demo-ai-hint">
        演示环境 AI 已配置 / 本地版可自行配置
      </div>

      <template v-if="!ai.ai_config_locked">
        <label class="fm-label">AI 服务地址（Base URL）
          <input v-model="ai.ai_base_url" placeholder="如 https://api.openai.com/v1 、https://api.deepseek.com/v1">
        </label>
        <label class="fm-label">API Key
          <input type="password" v-model="ai.ai_api_key" autocomplete="off"
                 :placeholder="ai.has_key ? ('已保存 ' + ai.ai_api_key_masked + '，留空保存则不修改') : '未配置，填写 sk- 开头的 Key'">
        </label>
        <label class="fm-label">模型名称
          <input v-model="ai.ai_model" placeholder="如 gpt-4o-mini / deepseek-chat / qwen-plus">
        </label>
      </template>

      <label class="set-switch">
        <input type="checkbox" v-model="ai.offline_mode">
        <span>离线模式：关闭所有 AI 功能（构架/精炼入口禁用），数据完全不发出本机</span>
      </label>

      <div class="ai-test-result" v-if="aiTest.msg" :class="aiTest.ok ? 'ok' : 'fail'">
        {{ aiTest.ok ? '✅ ' : '❌ ' }}{{ aiTest.msg }}
      </div>

      <div class="set-actions">
        <button class="btn" @click="clearAiKey" :disabled="ai.saving || !ai.has_key || ai.ai_config_locked">清除已保存的 Key</button>
        <button class="btn" @click="testAi" :disabled="ai.saving || ai.testing">
          {{ ai.testing ? '测试中…' : '测试连接' }}
        </button>
        <button class="btn btn-primary" @click="saveAi" :disabled="ai.saving || ai.ai_config_locked">
          {{ ai.saving ? '保存中…' : '保存 AI 配置' }}
        </button>
      </div>
    </div>

    <!-- ========== 数据库连接（卡12，演示模式隐藏） ========== -->
    <div class="set-card" v-if="!demoMode">
      <h3 class="set-h3">数据库连接（只读取数）</h3>
      <p class="set-desc">
        配置 MySQL / PostgreSQL 连接，从业务库<b>只读</b>取数落地为本地 CSV 数据文件。
        密码使用 Fernet 加密后存于本地 SQLite（密钥在 data/secret.key），绝不明文落库；
        仅允许 SELECT 查询，禁止 INSERT/UPDATE/DELETE/DROP 等写操作，无 LIMIT 时自动补 LIMIT 10000。
      </p>

      <div class="db-conn-form">
        <div class="db-row">
          <label class="fm-label db-lab">类型
            <select v-model="dbForm.type">
              <option value="mysql">MySQL</option>
              <option value="postgresql">PostgreSQL</option>
            </select>
          </label>
          <label class="fm-label db-lab">主机
            <input v-model="dbForm.host" placeholder="如 127.0.0.1">
          </label>
          <label class="fm-label db-lab">端口
            <input type="number" v-model.number="dbForm.port" :placeholder="dbForm.type==='postgresql'?5432:3306">
          </label>
        </div>
        <div class="db-row">
          <label class="fm-label db-lab">用户名
            <input v-model="dbForm.user" placeholder="数据库账号">
          </label>
          <label class="fm-label db-lab">密码
            <input type="password" v-model="dbForm.password" autocomplete="off" placeholder="连接密码">
          </label>
          <label class="fm-label db-lab">数据库名
            <input v-model="dbForm.db_name" placeholder="库名">
          </label>
        </div>
        <label class="fm-label">备注
          <input v-model="dbForm.note" placeholder="可选，如「生产订单库只读账号」">
        </label>

        <div class="db-test-result" v-if="dbTest.msg" :class="dbTest.ok ? 'ok' : 'fail'">
          {{ dbTest.ok ? '✅ ' : '❌ ' }}{{ dbTest.msg }}
        </div>

        <div class="set-actions">
          <button class="btn" @click="testDb" :disabled="db.testing">
            {{ db.testing ? '测试中…' : '测试连接' }}
          </button>
          <button class="btn btn-primary" @click="saveDb" :disabled="db.saving">
            {{ db.saving ? '保存中…' : '保存连接' }}
          </button>
        </div>
      </div>

      <h3 class="set-h3" style="margin-top:18px">已保存连接（{{ dbConns.length }}）</h3>
      <table class="data-table" v-if="dbConns.length">
        <thead>
          <tr><th>类型</th><th>主机</th><th>端口</th><th>库名</th><th>用户</th><th>备注</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in dbConns" :key="c.id">
            <td><span class="badge" :class="c.type">{{ c.type==='mysql'?'MySQL':'PostgreSQL' }}</span></td>
            <td>{{ c.host }}</td>
            <td>{{ c.port }}</td>
            <td>{{ c.db_name }}</td>
            <td>{{ c.user }}</td>
            <td>{{ c.note || '—' }}</td>
            <td><button class="btn btn-mini btn-danger" @click="delDb(c.id)">删除</button></td>
          </tr>
        </tbody>
      </table>
      <div v-else class="text-sub" style="padding:12px 0">尚未保存任何数据库连接</div>
    </div>

    <!-- ========== 行为埋点日志（卡13） ========== -->
    <div class="set-card">
      <h3 class="set-h3">行为埋点日志</h3>
      <p class="set-desc">
        系统自动记录本地行为日志（文件导入、体检查看、清洗执行、预设套用/保存、片段检索/复制/沉淀、
        需求单创建/确认、文档上传/检索、报告模板插入、AI 构架/精炼、报告导出等 16 类事件），
        全部仅存储于本地 SQLite，不上传任何服务器。可用于复盘自己的工作习惯与提效指标统计。
      </p>
      <div class="set-actions">
        <button class="btn btn-primary" @click="exportEvents" :disabled="exporting">
          {{ exporting ? '导出中…' : '⬇ 导出埋点日志（JSON）' }}
        </button>
      </div>
      <div class="ai-test-result" v-if="evMsg" :class="evOk ? 'ok' : 'fail'">{{ evMsg }}</div>
    </div>
  </div>
  `,
  setup() {
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

    const form = reactive({ w_urgency: 0.5, w_deadline: 0.3, w_requester: 0.2 });
    const defaults = { w_urgency: 0.5, w_deadline: 0.3, w_requester: 0.2 };
    const saving = ref(false);

    function valid(w) { return typeof w === 'number' && !isNaN(w) && w >= 0 && w <= 1; }
    const previewOk = ref(false);
    const preview = reactive({ highOver: 0, lowFar: 0 });
    function refreshPreview() {
      if (valid(form.w_urgency) && valid(form.w_deadline) && valid(form.w_requester)) {
        preview.highOver = 3 * form.w_urgency + 4 * form.w_deadline + 1.5 * form.w_requester;
        preview.lowFar = 1 * form.w_urgency + 1 * form.w_deadline + 1.5 * form.w_requester;
        previewOk.value = true;
      } else {
        previewOk.value = false;
      }
    }

    async function load() {
      const body = await api('/api/settings/sort-weights');
      if (body.code === 0) {
        Object.assign(form, body.data.weights);
        refreshPreview();
      }
    }

    function resetDefaults() {
      Object.assign(form, defaults);
      refreshPreview();
    }

    async function save() {
      if (!valid(form.w_urgency) || !valid(form.w_deadline) || !valid(form.w_requester)) {
        showToast('三项权重都必须是 0~1 之间的数字', 'error');
        return;
      }
      saving.value = true;
      const body = await api('/api/settings/sort-weights', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          w_urgency: form.w_urgency,
          w_deadline: form.w_deadline,
          w_requester: form.w_requester,
        }),
      });
      saving.value = false;
      if (body.code !== 0) { showToast(body.msg || '保存失败', 'error'); return; }
      Object.assign(form, body.data.weights);
      refreshPreview();
      showToast('权重已保存，需求清单已按新规则重排', 'success');
    }

    // ---------- AI 构架/精炼配置（卡11） ----------
    const ai = reactive({
      ai_base_url: '', ai_api_key: '', ai_model: '',
      offline_mode: false, has_key: false, ai_api_key_masked: '',
      saving: false, testing: false,
      ai_config_locked: false,  // 服务端环境变量锁定时隐藏录入框
    });
    const demoMode = ref(false);  // 演示模式：隐藏数据库直连入口
    const aiTest = reactive({ msg: '', ok: false });

    async function loadAi() {
      const body = await api('/api/settings/ai');
      if (body.code === 0 && body.data) {
        ai.ai_base_url = body.data.ai_base_url || '';
        ai.ai_model = body.data.ai_model || '';
        ai.offline_mode = !!body.data.offline_mode;
        ai.has_key = !!body.data.has_key;
        ai.ai_api_key_masked = body.data.ai_api_key_masked || '';
        ai.ai_config_locked = !!body.data.ai_config_locked;
        demoMode.value = !!body.data.demo_mode;
        ai.ai_api_key = '';  // 明文 Key 永不下发，输入框留空=不修改
      }
    }

    function aiPayload() {
      const p = {
        ai_base_url: ai.ai_base_url.trim(),
        ai_model: ai.ai_model.trim(),
        offline_mode: ai.offline_mode,
      };
      if (ai.ai_api_key) p.ai_api_key = ai.ai_api_key.trim();  // 留空=不修改
      return p;
    }

    async function saveAi() {
      if (!ai.offline_mode) {
        if (!ai.ai_base_url.trim() && !ai.has_key) { /* 允许保存空配置（尚未使用） */ }
        if (ai.ai_base_url.trim() && !/^https?:\/\//i.test(ai.ai_base_url.trim())) {
          showToast('Base URL 需以 http:// 或 https:// 开头', 'error');
          return;
        }
      }
      ai.saving = true;
      const body = await api('/api/settings/ai', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(aiPayload()),
      });
      ai.saving = false;
      if (body.code !== 0) { showToast(body.msg || '保存失败', 'error'); return; }
      Object.assign(ai, {
        has_key: body.data.has_key, ai_api_key_masked: body.data.ai_api_key_masked,
        offline_mode: body.data.offline_mode,
      });
      ai.ai_api_key = '';
      aiTest.msg = '';
      showToast('AI 配置已保存到本地数据库', 'success');
    }

    async function testAi() {
      ai.testing = true;
      aiTest.msg = '';
      const body = await api('/api/settings/ai/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(aiPayload()),
      });
      ai.testing = false;
      if (body.code !== 0) {
        aiTest.ok = false;
        aiTest.msg = body.msg || '测试请求失败';
        showToast(aiTest.msg, 'error');
        return;
      }
      aiTest.ok = !!body.data.ok;
      aiTest.msg = body.data.msg || (body.data.ok ? '连接成功' : '连接失败');
      showToast(aiTest.msg, body.data.ok ? 'success' : 'error');
    }

    async function clearAiKey() {
      if (!window.confirm('确定清除已保存的 API Key？清除后 AI 功能将不可用，直到重新填写。')) return;
      ai.saving = true;
      const body = await api('/api/settings/ai', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ai_api_key: '' }),
      });
      ai.saving = false;
      if (body.code !== 0) { showToast(body.msg || '清除失败', 'error'); return; }
      ai.has_key = false; ai.ai_api_key_masked = ''; ai.ai_api_key = '';
      showToast('已清除本地保存的 API Key', 'success');
    }

    // ---------- 数据库连接（卡12） ----------
    const dbForm = reactive({
      type: 'mysql', host: '', port: 3306, user: '',
      password: '', db_name: '', note: '',
    });
    const db = reactive({ saving: false, testing: false });
    const dbTest = reactive({ msg: '', ok: false });
    const dbConns = ref([]);

    async function loadDbConns() {
      const body = await api('/api/db/connections');
      if (body.code === 0) dbConns.value = body.data || [];
    }

    function dbPayload() {
      return {
        type: dbForm.type,
        host: dbForm.host.trim(),
        port: Number(dbForm.port) || (dbForm.type === 'postgresql' ? 5432 : 3306),
        user: dbForm.user.trim(),
        password: dbForm.password,
        db_name: dbForm.db_name.trim(),
        note: dbForm.note.trim(),
      };
    }

    async function testDb() {
      if (!dbForm.host.trim()) { showToast('请填写主机地址', 'error'); return; }
      db.testing = true; dbTest.msg = '';
      const body = await api('/api/db/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(dbPayload()),
      });
      db.testing = false;
      if (body.code !== 0) {
        dbTest.ok = false; dbTest.msg = body.msg || '测试请求失败';
        showToast(dbTest.msg, 'error'); return;
      }
      dbTest.ok = !!body.data.ok;
      dbTest.msg = body.data.msg || (body.data.ok ? '连接成功' : '连接失败');
      showToast(dbTest.msg, body.data.ok ? 'success' : 'error');
    }

    async function saveDb() {
      if (!dbForm.host.trim()) { showToast('请填写主机地址', 'error'); return; }
      if (!dbForm.user.trim()) { showToast('请填写用户名', 'error'); return; }
      if (!dbForm.db_name.trim()) { showToast('请填写数据库名', 'error'); return; }
      db.saving = true;
      const body = await api('/api/db/connections', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(dbPayload()),
      });
      db.saving = false;
      if (body.code !== 0) { showToast(body.msg || '保存失败', 'error'); return; }
      // 清空密码框（不回显），其余保留便于连续添加
      dbForm.password = '';
      dbTest.msg = '';
      showToast('连接已保存（密码已加密存储）', 'success');
      loadDbConns();
    }

    async function delDb(cid) {
      if (!window.confirm('确定删除该数据库连接？')) return;
      const body = await api('/api/db/connections/' + cid, { method: 'DELETE' });
      if (body.code !== 0) { showToast(body.msg || '删除失败', 'error'); return; }
      showToast('连接已删除', 'success');
      loadDbConns();
    }

    // ---------- 行为埋点日志导出（卡13） ----------
    const exporting = ref(false);
    const evMsg = ref('');
    const evOk = ref(false);

    async function exportEvents() {
      exporting.value = true; evMsg.value = '';
      try {
        const res = await fetch('/api/settings/events/export');
        const cd = res.headers.get('content-disposition') || '';
        if (!res.ok || !cd.includes('attachment')) {
          // 无附件头 = 业务失败（统一 JSON 响应）
          const body = await res.json().catch(() => ({}));
          evOk.value = false;
          evMsg.value = body.msg || '导出失败，请重试';
          return;
        }
        const blob = await res.blob();
        let name = 'liteops_events.json';
        const m = cd.match(/filename\*=utf-8''([^;]+)/i);
        if (m) { try { name = decodeURIComponent(m[1]); } catch { name = m[1]; } }
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = name;
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
        evOk.value = true;
        evMsg.value = '已导出 ' + name + '（共 ' + JSON.parse(await blob.text()).total_cnt + ' 条事件）';
      } catch (e) {
        evOk.value = false;
        evMsg.value = '导出失败，请确认服务已启动后重试';
      } finally {
        exporting.value = false;
      }
    }

    // 输入即刷新预览
    watch(form, refreshPreview, { deep: true });

    onMounted(() => { load(); loadAi(); loadDbConns(); });

    return {
      toast, form, saving, previewOk, preview, resetDefaults, save,
      ai, aiTest, saveAi, testAi, clearAiKey, demoMode,
      dbForm, db, dbTest, dbConns, testDb, saveDb, delDb, loadDbConns,
      exporting, evMsg, evOk, exportEvents,
    };
  },
};
})();
