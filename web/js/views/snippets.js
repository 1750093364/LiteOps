/* SQL 片段库视图（卡06）。
   左侧分类树（含计数）+ 顶部关键字搜索 + 右侧片段卡片；
   关键字黄色高亮、一键复制（调 /copy 计数后写剪贴板）、插入到报告（派发自定义事件）；
   支持自建片段的新建 / 编辑 / 删除（内置片段受后端保护）。
   Vue3 全局构建，无构建步骤。整体包在 IIFE 内，避免全局 const 冲突。 */
(function () {
const { ref, reactive, computed, onMounted } = Vue;

const CATEGORIES = ['留存', '漏斗', 'TopN', '同环比', '去重', '汇总', '其他'];
const DIALECTS = ['mysql', 'postgresql', 'hive', 'sqlserver', 'clickhouse'];
const PAGE_SIZE = 20;

window.SnippetsView = {
  name: 'SnippetsView',
  template: `
  <div class="snp-view">
    <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.msg }}</div>

    <div class="view-head">
      <div>
        <h1>SQL 片段库</h1>
        <p>留存 / 漏斗 / TopN 等高频分析 SQL 沉淀：改表名直接跑，一键复制或插入报告</p>
      </div>
      <button class="btn btn-primary" @click="openCreate">＋ 新建片段</button>
    </div>

    <div class="snp-toolbar">
      <input class="search-input snp-search" v-model="kw"
             placeholder="搜索标题 / SQL 正文 / 注释，如：留存、漏斗、ROW_NUMBER…"
             @input="onSearchInput">
      <select class="cat-select snp-dialect" v-model="dialect" @change="reload">
        <option value="">全部方言</option>
        <option v-for="d in dialects" :key="d" :value="d">{{ d }}</option>
      </select>
    </div>

    <div class="snp-layout">
      <!-- 左侧分类树 -->
      <aside class="snp-tree">
        <div class="snp-tree-item" :class="{ active: cat === '' }" @click="selectCat('')">
          <span>全部</span><span class="snp-cnt">{{ cats.total }}</span>
        </div>
        <div v-for="c in cats.categories" :key="c.name" class="snp-tree-item"
             :class="{ active: cat === c.name }" @click="selectCat(c.name)">
          <span>{{ c.name }}</span><span class="snp-cnt">{{ c.cnt }}</span>
        </div>
      </aside>

      <!-- 右侧片段卡片 -->
      <div class="snp-main">
        <div v-if="loading" class="empty">加载中…</div>
        <div v-else-if="list.length === 0" class="empty">没有匹配的片段，换个关键字或分类试试</div>
        <div v-else class="snp-list">
          <div v-for="item in list" :key="item.id" class="snp-card">
            <div class="snp-card-head">
              <span class="snp-title" v-html="hl(item.title)"></span>
              <span class="badge snp-badge-dialect">{{ item.dialect }}</span>
              <span class="badge hint">{{ item.category }}</span>
              <span class="badge" :class="item.source === 'builtin' ? 'status' : 'manual'">
                {{ item.source === 'builtin' ? '内置' : '自建' }}
              </span>
              <span class="snp-usecnt">已复制 {{ item.use_count }} 次</span>
            </div>
            <pre class="snp-code"><code v-html="hl(item.sql_body)"></code></pre>
            <div class="snp-note" v-if="item.note">
              <span class="snp-note-label">说明</span>
              <span v-html="hl(item.note)"></span>
            </div>
            <div class="snp-ops">
              <button class="btn btn-mini btn-primary" @click="copySnip(item)">
                {{ copiedId === item.id ? '✓ 已复制' : '复制 SQL' }}
              </button>
              <button class="btn btn-mini" @click="insertSnip(item)">插入到报告</button>
              <button class="snp-text-btn" @click="openEdit(item)">编辑</button>
              <button class="snp-text-btn danger" @click="removeSnip(item)">删除</button>
            </div>
          </div>
        </div>

        <div class="snp-pager" v-if="total > pageSize">
          <button class="btn btn-mini" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
          <span>第 {{ page }} 页 / 共 {{ totalPages }} 页（{{ total }} 条）</span>
          <button class="btn btn-mini" :disabled="page >= totalPages" @click="goPage(page + 1)">下一页</button>
        </div>
      </div>
    </div>

    <!-- 新建 / 编辑片段弹窗 -->
    <div class="modal-mask" v-if="modal.show" @click.self="modal.show = false">
      <div class="modal">
        <h3>{{ modal.id ? '编辑片段' : '新建片段' }}</h3>
        <label class="fm-label">标题
          <input v-model="modal.title" placeholder="如：近30天加购未支付用户">
        </label>
        <label class="fm-label">分类 / 方言
          <div class="flex-row">
            <select class="cat-select" v-model="modal.category">
              <option v-for="c in categoryList" :key="c" :value="c">{{ c }}</option>
            </select>
            <select class="cat-select" v-model="modal.dialect">
              <option v-for="d in dialects" :key="d" :value="d">{{ d }}</option>
            </select>
          </div>
        </label>
        <label class="fm-label">SQL 正文（支持 -- 注释，保留换行）
          <textarea v-model="modal.sql_body" class="snp-sql-input" rows="12"
                    placeholder="粘贴可直接改表名运行的 SQL…"></textarea>
        </label>
        <label class="fm-label">注释说明
          <textarea v-model="modal.note" rows="2"
                    placeholder="业务含义、使用场景、参数说明…"></textarea>
        </label>
        <div class="modal-foot">
          <button class="btn" @click="modal.show = false">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="saveSnip">
            {{ saving ? '保存中…' : '保存' }}
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

    // ---------- 列表 / 筛选 ----------
    const kw = ref('');
    const cat = ref('');
    const dialect = ref('');
    const list = ref([]);
    const total = ref(0);
    const page = ref(1);
    const pageSize = PAGE_SIZE;
    const loading = ref(false);
    const copiedId = ref(null);
    const saving = ref(false);
    const cats = ref({
      total: 0,
      categories: CATEGORIES.map(name => ({ name, cnt: 0 })),
    });
    const categoryList = CATEGORIES;
    const dialects = DIALECTS;
    const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

    let searchTimer = null;
    function onSearchInput() {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { page.value = 1; load(); }, 300);  // 输入防抖
    }
    function selectCat(c) { cat.value = c; page.value = 1; load(); }
    function reload() { page.value = 1; load(); }
    function goPage(p) { page.value = p; load(); }

    async function load() {
      loading.value = true;
      const params = new URLSearchParams();
      if (kw.value.trim()) params.set('q', kw.value.trim());
      if (cat.value) params.set('category', cat.value);
      if (dialect.value) params.set('dialect', dialect.value);
      params.set('page', page.value);
      params.set('page_size', pageSize);
      const body = await api('/api/snippets?' + params.toString());
      loading.value = false;
      if (body.code !== 0) { showToast(body.msg || '片段列表加载失败', 'error'); return; }
      list.value = body.data.list;
      total.value = body.data.total;
    }

    async function loadCats() {
      const body = await api('/api/snippets/categories');
      if (body.code === 0) cats.value = body.data;
    }

    // ---------- 关键字高亮（先转义再替换，防 XSS / 破坏排版） ----------
    function escHtml(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    function hl(text) {
      const safe = escHtml(text);
      const k = escHtml(kw.value.trim());
      if (!k) return safe;
      const pat = k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      return safe.replace(new RegExp('(' + pat + ')', 'gi'),
        '<mark class="snp-hl">$1</mark>');
    }

    // ---------- 复制（调 /copy 计数 → 写剪贴板） ----------
    async function copySnip(item) {
      const body = await api('/api/snippets/' + item.id + '/copy', { method: 'POST' });
      if (body.code !== 0) { showToast(body.msg || '复制失败', 'error'); return; }
      const sql = body.data.sql_body || '';
      let ok = false;
      try {
        await navigator.clipboard.writeText(sql);  // 127.0.0.1 属安全上下文
        ok = true;
      } catch (e) {
        // 兜底：旧浏览器 / 非安全上下文用 textarea + execCommand
        const ta = document.createElement('textarea');
        ta.value = sql;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
        document.body.removeChild(ta);
      }
      item.use_count = body.data.use_count;
      copiedId.value = item.id;
      setTimeout(() => { if (copiedId.value === item.id) copiedId.value = null; }, 2000);
      showToast(ok
        ? 'SQL 已复制到剪贴板（方言：' + body.data.dialect + '）'
        : '复制失败，请手动选择代码块复制',
        ok ? 'success' : 'error');
    }

    // ---------- 插入到报告（报告编辑器监听 liteops:insert-snippet） ----------
    // 视图互斥挂载：报告编辑器未打开时经 sessionStorage 暂存 + 跳转报告工作台，
    // 由报告视图打开编辑器时读取暂存并插入光标处。
    function insertSnip(item) {
      const detail = { sql: item.sql_body, title: item.title };
      try {
        sessionStorage.setItem('liteops:pending-snippet', JSON.stringify(detail));
      } catch (e) { /* 隐私模式等场景忽略暂存失败 */ }
      window.dispatchEvent(new CustomEvent('liteops:insert-snippet', { detail }));
      window.dispatchEvent(new CustomEvent('liteops:goto', { detail: 'reports' }));
      showToast('已跳转到报告工作台，SQL 将插入打开的报告编辑器', 'success');
    }

    // ---------- 新建 / 编辑 / 删除 ----------
    const modal = reactive({
      show: false, id: null, title: '', category: '其他',
      dialect: 'mysql', sql_body: '', note: '',
    });

    function openCreate() {
      Object.assign(modal, {
        show: true, id: null, title: '', category: '其他',
        dialect: 'mysql', sql_body: '', note: '',
      });
    }
    function openEdit(item) {
      Object.assign(modal, {
        show: true, id: item.id, title: item.title, category: item.category,
        dialect: item.dialect, sql_body: item.sql_body, note: item.note || '',
      });
    }
    async function saveSnip() {
      if (!modal.title.trim()) { showToast('请填写片段标题', 'error'); return; }
      if (!modal.sql_body.trim()) { showToast('请填写 SQL 正文', 'error'); return; }
      saving.value = true;
      const payload = {
        title: modal.title.trim(),
        category: modal.category,
        dialect: modal.dialect,
        sql_body: modal.sql_body,
        note: modal.note.trim(),
      };
      const path = modal.id ? '/api/snippets/' + modal.id : '/api/snippets';
      const body = await api(path, {
        method: modal.id ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      saving.value = false;
      if (body.code !== 0) { showToast(body.msg || '保存失败', 'error'); return; }
      modal.show = false;
      showToast(modal.id ? '片段已更新' : '片段已保存', 'success');
      load();
      loadCats();
    }
    async function removeSnip(item) {
      if (!window.confirm('确定删除片段《' + item.title + '》？此操作不可恢复')) return;
      const body = await api('/api/snippets/' + item.id, { method: 'DELETE' });
      if (body.code !== 0) { showToast(body.msg || '删除失败', 'error'); return; }
      showToast('片段已删除', 'success');
      load();
      loadCats();
    }

    onMounted(() => { load(); loadCats(); });

    return {
      toast, kw, cat, dialect, list, total, page, pageSize, loading,
      cats, copiedId, saving, modal, categoryList, dialects, totalPages,
      onSearchInput, selectCat, reload, goPage, hl, copySnip, insertSnip,
      openCreate, openEdit, saveSnip, removeSnip,
    };
  },
};
})();
