/* 数据存储视图（卡02）：文件导入 + 目录树 + 全文检索 + 数据预览（字段类型修正/标签/备注）。
   Vue3 全局构建，无构建步骤。整体包在 IIFE 内，避免与 app.js 在全局作用域重复 const 声明。 */
(function () {
const { ref, reactive, computed, onMounted } = Vue;

// 字段类型可选值（与后端 FIELD_TYPES 一致）
const FIELD_TYPES = ['整数', '小数', '日期', '布尔', '文本', '混合'];

window.FilesView = {
  name: 'FilesView',
  template: `
  <div class="files-view">
    <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.msg }}</div>

    <!-- ================= 列表页 ================= -->
    <template v-if="mode === 'list'">
      <div class="view-head">
        <div>
          <h1>数据存储</h1>
          <p>导入 Excel / CSV 数据文件，自动识别编码与字段类型</p>
        </div>
        <button class="btn" @click="openDbModal" :disabled="dbModal.busy">从数据库取数</button>
        <button class="btn btn-primary" :disabled="uploading" @click="pickFile">
          {{ uploading ? '导入中…' : '上传文件' }}
        </button>
        <input ref="fileInput" type="file" accept=".xlsx,.xls,.csv" style="display:none"
               @change="onInputChange" />
      </div>

      <!-- 产品边界声明（PRD 第 5 章 Out of Scope，卡13 收口） -->
      <div class="scope-card">
        <div class="scope-main">
          <span class="scope-badge">产品边界</span>
          <span><b>轻析不做看板与可视化大屏</b>——做 BI 之外的事：临时取数、清洗、报告与知识沉淀。</span>
        </div>
        <div class="scope-list">
          <span class="scope-item">✕ BI 看板 / 拖拽可视化大屏</span>
          <span class="scope-item">✕ 数据仓库建模 / ETL 调度 / 实时数据流</span>
          <span class="scope-item">✕ 统计建模引擎（仅提供框架模板）</span>
          <span class="scope-item">✕ 多人实时协同 / 细粒度角色权限</span>
        </div>
      </div>

      <div class="catalog-layout">
        <!-- 左侧目录树 -->
        <aside class="catalog-tree">
          <div class="tree-tabs">
            <button class="tree-tab" :class="{active: treeView==='type'}" @click="switchTree('type')">按类型</button>
            <button class="tree-tab" :class="{active: treeView==='custom'}" @click="switchTree('custom')">我的分类</button>
          </div>

          <div class="tree-actions" v-if="treeView==='custom'">
            <button class="btn btn-mini" @click="showCatForm=true">+ 新建分类</button>
          </div>
          <div class="cat-form" v-if="showCatForm">
            <input v-model="newCatName" placeholder="分类名称" class="cat-input" @keyup.enter="createCategory" />
            <select v-model="newCatParent" class="cat-select">
              <option :value="null">顶级分类</option>
              <option v-for="c in topCats" :key="c.id" :value="c.id">{{ c.name }}</option>
            </select>
            <button class="btn btn-mini btn-primary" @click="createCategory">创建</button>
            <button class="btn btn-mini" @click="showCatForm=false;newCatName=''">取消</button>
          </div>

          <div class="tree-body">
            <!-- type 视图 -->
            <template v-if="treeView==='type'">
              <div class="tree-node" v-for="g in typeGroups" :key="g.key"
                   :class="{active: activeFilter.type===g.key}" @click="selectTypeGroup(g.key)">
                <span class="tree-label">{{ g.label }}</span>
                <span class="tree-count">{{ g.count }}</span>
              </div>
              <div class="cleaned-items" v-if="cleanedItems.length">
                <div class="cleaned-item" v-for="it in cleanedItems" :key="it.id" @click="openPreview(it.id)">
                  <div class="cleaned-name">{{ it.origin_name }}</div>
                  <div class="cleaned-parent">来源：{{ it.parent_name || '（未知）' }}</div>
                </div>
              </div>
            </template>

            <!-- custom 视图 -->
            <template v-else>
              <div class="tree-node" :class="{active: activeFilter.uncategorized}" @click="selectUncategorized">
                <span class="tree-label">未分类</span>
                <span class="tree-count">{{ treeCustom?.uncategorized || 0 }}</span>
              </div>
              <template v-for="c in treeCustom?.categories || []" :key="c.id">
                <div class="tree-node" :class="{active: activeFilter.catId===c.id}" @click="selectCategory(c.id)">
                  <span class="tree-label">{{ c.name }}</span>
                  <span class="tree-count">{{ c.count }}</span>
                  <button class="tree-del" v-if="c.count===0" @click.stop="delCategory(c.id)" title="删除分类">×</button>
                </div>
                <div class="tree-node child" v-for="cc in c.children" :key="cc.id"
                     :class="{active: activeFilter.catId===cc.id}" @click="selectCategory(cc.id)">
                  <span class="tree-label">└ {{ cc.name }}</span>
                  <span class="tree-count">{{ cc.count }}</span>
                  <button class="tree-del" v-if="cc.count===0" @click.stop="delCategory(cc.id)" title="删除分类">×</button>
                </div>
              </template>
            </template>
          </div>
        </aside>

        <!-- 右侧内容区 -->
        <div class="catalog-main">
          <!-- 搜索框 -->
          <div class="search-bar">
            <input v-model="searchKw" class="search-input" placeholder="搜索文件名 / 字段名 / 备注 / 标签"
                   @keyup.enter="doSearch" />
            <button class="btn btn-primary btn-mini" @click="doSearch">搜索</button>
            <button class="btn btn-mini" v-if="searchKw" @click="clearSearch">清除</button>
            <span class="search-info" v-if="searchKw">命中 {{ searchResults.total }} 个文件</span>
          </div>

          <div class="drop-zone" :class="{ over: dragOver, uploading }"
               @click="pickFile"
               @dragover.prevent="dragOver = true"
               @dragleave.prevent="dragOver = false"
               @drop.prevent="onDrop">
            <div class="drop-icon">⇪</div>
            <div class="drop-text">
              {{ uploading ? '正在导入，请稍候…' : '点击选择或拖拽文件到此处上传' }}
            </div>
            <div class="drop-hint">支持 .xlsx / .xls / .csv · CSV 上限 500MB/100万行 · Excel 上限 50MB</div>
          </div>

          <!-- 搜索结果视图 -->
          <template v-if="searchMode">
            <table class="data-table">
              <thead>
                <tr><th>文件名</th><th>命中字段</th><th>类型</th><th>行数</th><th>操作</th></tr>
              </thead>
              <tbody>
                <tr v-if="searchLoading"><td colspan="5" class="empty">搜索中…</td></tr>
                <tr v-else-if="searchResults.list.length===0"><td colspan="5" class="empty">未找到匹配结果</td></tr>
                <tr v-for="item in searchResults.list" :key="item.id">
                  <td class="strong link" @click="openPreview(item.id)"><span v-html="hl(item.origin_name)"></span></td>
                  <td class="hit-fields">
                    <span class="badge hit" v-for="hf in item.hit_fields" :key="hf" v-html="hl(hf)"></span>
                    <span v-if="!item.hit_fields.length" class="text-sub">（文件名/备注/标签命中）</span>
                  </td>
                  <td><span class="badge" :class="item.file_type">{{ typeText(item.file_type) }}</span></td>
                  <td>{{ fmtNum(item.row_count) }}</td>
                  <td><button class="btn btn-mini" @click="openPreview(item.id)">预览</button></td>
                </tr>
              </tbody>
            </table>
          </template>

          <!-- 普通列表视图 -->
          <template v-else>
            <table class="data-table">
              <thead>
                <tr>
                  <th>文件名</th><th>类型</th><th>行数</th><th>列数</th>
                  <th>大小</th><th>分类</th><th>标签</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="loading"><td colspan="8" class="empty">加载中…</td></tr>
                <tr v-else-if="list.length === 0"><td colspan="8" class="empty">暂无文件，先上传一个吧</td></tr>
                <tr v-for="item in list" :key="item.id">
                  <td class="strong link" @click="openPreview(item.id)">{{ item.origin_name }}</td>
                  <td><span class="badge" :class="item.file_type">{{ typeText(item.file_type) }}</span></td>
                  <td>{{ fmtNum(item.row_count) }}</td>
                  <td>{{ fmtNum(item.col_count) }}</td>
                  <td>{{ fmtSize(item.size_bytes) }}</td>
                  <td>
                    <select class="cat-cell-select" :value="item.category_id || ''"
                            @change="moveFileCategory(item, $event.target.value)">
                      <option value="">未分类</option>
                      <optgroup v-for="c in flatCats" :key="c.id" :label="c.name">
                        <option :value="c.id">{{ c.name }}</option>
                        <option v-for="cc in c.children" :key="cc.id" :value="cc.id">└ {{ cc.name }}</option>
                      </optgroup>
                    </select>
                  </td>
                  <td class="tag-cell">
                    <span class="tag" v-for="t in item.tags" :key="t">{{ t }}</span>
                    <span v-if="!item.tags.length" class="text-sub">—</span>
                  </td>
                  <td class="ops">
                    <button class="btn btn-mini" @click="openPreview(item.id)">预览</button>
                    <button class="btn btn-mini btn-danger" @click="del(item)">删除</button>
                  </td>
                </tr>
              </tbody>
            </table>

            <div class="pager" v-if="total > 0">
              <span>共 {{ total }} 条 · 第 {{ page }}/{{ totalPages }} 页</span>
              <button class="btn btn-mini" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
              <button class="btn btn-mini" :disabled="page >= totalPages" @click="goPage(page + 1)">下一页</button>
            </div>
          </template>
        </div>
      </div>

      <!-- ========== 从数据库取数弹窗（卡12） ========== -->
      <div class="modal-mask" v-if="dbModal.show" @click.self="closeDbModal">
        <div class="modal db-modal">
          <div class="modal-head">
            <span>从数据库取数</span>
            <button class="modal-close" @click="closeDbModal">×</button>
          </div>
          <div class="modal-body">
            <div class="db-pick-row">
              <label class="fm-label">选择连接
                <select v-model="dbModal.cid" @change="onConnChange">
                  <option :value="null">请选择连接</option>
                  <option v-for="c in dbModal.conns" :key="c.id" :value="c.id">
                    {{ c.type==='mysql'?'MySQL':'PG' }} · {{ c.host }}:{{ c.port }}/{{ c.db_name }}（{{ c.user }}）
                  </option>
                </select>
              </label>
              <button class="btn btn-mini" @click="loadDbConns" :disabled="dbModal.busy">刷新</button>
              <a href="javascript:void(0)" class="db-link" @click="gotoSettings">去设置页配置连接 →</a>
            </div>

            <div class="db-mode-tabs" v-if="dbModal.cid">
              <button class="tree-tab" :class="{active: dbModal.mode==='table'}" @click="dbModal.mode='table'">按表取数</button>
              <button class="tree-tab" :class="{active: dbModal.mode==='sql'}" @click="dbModal.mode='sql'">SQL 查询</button>
            </div>

            <!-- 表模式 -->
            <template v-if="dbModal.cid && dbModal.mode==='table'">
              <div class="db-table-list" v-if="dbModal.tables.length">
                <div class="db-table-item" v-for="t in dbModal.tables" :key="t"
                     :class="{active: dbModal.previewTable===t}" @click="previewDbTable(t)">
                  {{ t }}
                </div>
              </div>
              <div v-else class="text-sub">{{ dbModal.loadingTables ? '加载表列表中…' : '该库无表' }}</div>

              <div class="db-preview" v-if="dbModal.previewTable">
                <div class="db-preview-head">
                  <b>表：{{ dbModal.previewTable }}</b>
                  <button class="btn btn-mini btn-primary" @click="importDbTable" :disabled="dbModal.busy">
                    {{ dbModal.busy ? '导入中…' : '导入此表' }}
                  </button>
                </div>
                <div class="table-wrap" style="max-height:320px;overflow:auto">
                  <table class="data-table preview-table" v-if="dbModal.previewCols.length">
                    <thead><tr><th v-for="c in dbModal.previewCols" :key="c">{{ c }}</th></tr></thead>
                    <tbody>
                      <tr v-for="(row,i) in dbModal.previewRows" :key="i">
                        <td v-for="(cell,j) in row" :key="j">{{ cell===''?'（空）':cell }}</td>
                      </tr>
                    </tbody>
                  </table>
                  <div v-else class="empty">点击表名预览前 100 行</div>
                </div>
              </div>
            </template>

            <!-- SQL 模式 -->
            <template v-if="dbModal.cid && dbModal.mode==='sql'">
              <textarea class="db-sql" v-model="dbModal.sql"
                        placeholder="仅允许 SELECT 查询，如：SELECT id, name FROM orders WHERE amount > 100"></textarea>
              <div class="set-actions">
                <button class="btn btn-primary" @click="importDbSql" :disabled="dbModal.busy">
                  {{ dbModal.busy ? '执行中…' : '执行取数并导入' }}
                </button>
                <span class="text-sub">仅 SELECT · 无 LIMIT 自动补 10000 · 禁写操作</span>
              </div>
            </template>

            <div v-if="dbModal.err" class="db-err">{{ dbModal.err }}</div>
          </div>
        </div>
      </div>
    </template>

    <!-- ================= 预览页 ================= -->
    <template v-else>
      <div class="view-head">
        <div class="flex-row">
          <button class="btn btn-mini" @click="backToList">← 返回列表</button>
          <h1 class="preview-title">{{ preview ? preview.origin_name : '数据预览' }}</h1>
          <span class="badge cleaned" v-if="preview && preview.parent_file_id">清洗产物</span>
        </div>
      </div>

      <div v-if="previewLoading" class="empty">加载中…</div>
      <template v-else-if="preview">
        <div class="meta-bar">
          <span class="meta-item"><label>类型</label>{{ typeText(preview.file_type) }}</span>
          <span class="meta-item"><label>行数</label>{{ fmtNum(preview.row_count) }}</span>
          <span class="meta-item"><label>列数</label>{{ fmtNum(preview.col_count) }}</span>
          <span class="meta-item"><label>大小</label>{{ fmtSize(preview.size_bytes) }}</span>
          <span class="meta-item"><label>状态</label>{{ preview.status }}</span>
          <span class="meta-item"><label>导入时间</label>{{ preview.created_at }}</span>
          <span class="meta-item" v-if="preview.parent_name">
            <label>来源文件</label>{{ preview.parent_name }}
          </span>
          <span class="meta-item" v-if="preview.file_type === 'csv'">
            <label>编码</label>
            <select class="enc-select" v-model="encInput">
              <option v-for="e in encodingOptions" :key="e" :value="e">{{ e }}</option>
            </select>
            <button class="btn btn-mini" @click="applyEncoding">重新解析</button>
          </span>
          <span class="meta-item" v-else><label>编码</label>—</span>
        </div>

        <!-- 标签与备注 -->
        <div class="note-panel">
          <div class="note-row">
            <label class="note-label">标签</label>
            <div class="tag-editor">
              <span class="tag removable" v-for="t in preview.tags" :key="t">
                {{ t }}<span class="tag-x" @click="removeTag(t)">×</span>
              </span>
              <input v-model="newTag" class="tag-input" placeholder="输入标签回车添加" @keyup.enter="addTag" />
            </div>
          </div>
          <div class="note-row">
            <label class="note-label">备注/口径</label>
            <div class="note-edit">
              <textarea v-model="noteDraft" class="note-textarea"
                        placeholder="填写数据口径、业务说明等（保存后生效）"></textarea>
              <button class="btn btn-mini btn-primary" @click="saveNote">保存备注</button>
            </div>
          </div>
        </div>

        <h2 class="section-title">字段概览（{{ preview.fields.length }} 个）</h2>
        <div class="field-grid">
          <div class="field-card" v-for="f in preview.fields" :key="f.id">
            <div class="field-head">
              <span class="field-name" :title="f.field_name">{{ f.field_name }}</span>
              <span class="badge type" :class="typeBase(f.display_type)">{{ typeBase(f.display_type) }}</span>
            </div>
            <div class="field-hints">
              <span class="badge hint" v-if="typeHint(f.inferred_type)">{{ typeHint(f.inferred_type) }}</span>
              <span class="badge manual" v-if="f.manual_type">已人工修正</span>
            </div>
            <div class="field-type-fix">
              <select class="type-select" :value="typeBase(f.display_type)" @change="fixFieldType(f, $event.target.value)">
                <option v-for="t in FIELD_TYPES" :key="t" :value="t">{{ t }}</option>
              </select>
              <button class="btn btn-mini" v-if="f.manual_type" @click="fixFieldType(f, null)">恢复推断</button>
            </div>
            <div class="miss-row">
              <div class="miss-bar"><div class="miss-fill"
                   :class="{ danger: f.missing_rate > 0.5 }"
                   :style="{ width: (f.missing_rate * 100) + '%' }"></div></div>
              <span class="miss-num" :class="{ danger: f.missing_rate > 0.5 }">
                {{ (f.missing_rate * 100).toFixed(1) }}%
              </span>
            </div>
            <div class="samples" :title="(f.sample_values || []).join('，')">
              示例：{{ (f.sample_values && f.sample_values.length) ? f.sample_values.join('，') : '（空列）' }}
            </div>
          </div>
        </div>

        <h2 class="section-title">数据预览（前 5 行）</h2>
        <div class="table-wrap">
          <table class="data-table preview-table">
            <thead>
              <tr><th class="row-idx">#</th><th v-for="c in preview.preview_columns" :key="c">{{ c }}</th></tr>
            </thead>
            <tbody>
              <tr v-if="previewRows.length === 0"><td :colspan="preview.preview_columns.length + 1" class="empty">无数据行</td></tr>
              <tr v-for="(row, i) in previewRows" :key="i">
                <td class="row-idx">{{ i + 1 }}</td>
                <td v-for="(cell, j) in row" :key="j" :class="{ 'cell-null': cell === '' }">
                  {{ cell === '' ? '（空）' : cell }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="text-sub" style="padding:8px 2px">
          共 {{ fmtNum(preview.row_count) }} 行，仅预览前 {{ previewRows.length }} 行
        </div>
      </template>
    </template>
  </div>
  `,
  setup() {
    // ---------- 通用 ----------
    const toast = reactive({ show: false, msg: '', type: 'info' });
    let toastTimer = null;
    function showToast(msg, type = 'info') {
      toast.show = true; toast.msg = msg; toast.type = type;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toast.show = false; }, 3000);
    }

    async function api(path, opts) {
      const res = await fetch(path, opts);
      try { return await res.json(); }
      catch { return { code: -1, msg: '服务响应异常' }; }
    }

    function fmtNum(n) { return n == null ? '—' : Number(n).toLocaleString('zh-CN'); }
    function fmtSize(b) {
      if (b == null) return '—';
      if (b < 1024) return b + ' B';
      if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
      return (b / 1024 / 1024).toFixed(2) + ' MB';
    }
    function typeText(t) {
      if (t === 'csv') return 'CSV';
      if (t === 'excel') return 'Excel';
      if (t === '数据库表') return '数据库表';
      return t || '—';
    }

    function typeBase(t) { return (t || '文本').split('·')[0]; }
    function typeHint(t) {
      const p = (t || '').split('·')[1];
      return p || '';
    }

    // 关键字高亮（返回 HTML）
    function hl(text) {
      const q = searchKw.value.trim();
      if (!q || text == null) return text;
      // 转义 HTML 特殊字符
      const esc = String(text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      const re = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
      return esc.replace(re, '<mark>$1</mark>');
    }

    // ---------- 列表 ----------
    const mode = ref('list');
    const list = ref([]);
    const total = ref(0);
    const page = ref(1);
    const pageSize = ref(10);
    const loading = ref(false);
    const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));

    // 当前筛选条件
    const activeFilter = reactive({ type: null, catId: null, uncategorized: false });

    async function loadList() {
      loading.value = true;
      let url = '/api/files?page=' + page.value + '&page_size=' + pageSize.value;
      if (activeFilter.catId != null) url += '&category_id=' + activeFilter.catId;
      if (activeFilter.type === 'excel') url += '&file_type=excel';
      if (activeFilter.type === 'csv') url += '&file_type=csv';
      // 清洗产物 / 数据库表 由 type 视图计数，列表筛选时通过 parent_file_id/source 过滤
      // 简化：type 视图点击时用 file_type 或 parent 标记
      const body = await api(url);
      loading.value = false;
      if (body.code !== 0) { showToast(body.msg || '列表加载失败', 'error'); return; }
      // type 视图特殊过滤
      let items = body.data.list;
      if (activeFilter.type === 'cleaned') {
        items = items.filter(it => it.parent_file_id != null);
      } else if (activeFilter.type === 'database') {
        items = items.filter(it => (it.source || '').indexOf('数据库') >= 0);
      }
      list.value = items;
      total.value = body.data.total;
    }

    function goPage(p) { page.value = p; loadList(); }

    // ---------- 目录树 ----------
    const treeView = ref('type');
    const treeType = ref(null);   // type 视图数据
    const treeCustom = ref(null);  // custom 视图数据
    const showCatForm = ref(false);
    const newCatName = ref('');
    const newCatParent = ref(null);

    const typeGroups = computed(() => (treeType.value?.groups || []));
    const cleanedItems = computed(() => {
      const g = typeGroups.value.find(g => g.key === 'cleaned');
      return g ? g.items : [];
    });
    const topCats = computed(() => (treeCustom.value?.categories || []).filter(c => !c.parent_id));
    // 扁平分类列表（用于文件行的分类下拉，始终加载 custom 视图数据）
    const flatCats = computed(() => (allCats.value || []));
    const allCats = ref([]);

    async function loadAllCats() {
      const body = await api('/api/files/tree?view=custom');
      if (body.code === 0) allCats.value = body.data.categories || [];
    }

    async function loadTree() {
      const body = await api('/api/files/tree?view=' + treeView.value);
      if (body.code !== 0) { showToast(body.msg || '目录树加载失败', 'error'); return; }
      if (treeView.value === 'type') treeType.value = body.data;
      else treeCustom.value = body.data;
    }

    function switchTree(v) {
      treeView.value = v;
      loadTree();
      if (v === 'type') selectTypeGroup(null);
      else selectCategory(null);
    }

    function selectTypeGroup(key) {
      activeFilter.type = key;
      activeFilter.catId = null;
      activeFilter.uncategorized = false;
      page.value = 1;
      loadList();
    }

    function selectCategory(cid) {
      activeFilter.catId = cid;
      activeFilter.type = null;
      activeFilter.uncategorized = false;
      page.value = 1;
      loadList();
    }

    function selectUncategorized() {
      activeFilter.uncategorized = true;
      activeFilter.catId = null;
      activeFilter.type = null;
      page.value = 1;
      loadList();
    }

    async function createCategory() {
      const name = newCatName.value.trim();
      if (!name) { showToast('请输入分类名称', 'error'); return; }
      const body = await api('/api/categories?scope=file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, parent_id: newCatParent.value || null }),
      });
      if (body.code !== 0) { showToast(body.msg || '创建分类失败', 'error'); return; }
      showToast('分类已创建', 'success');
      newCatName.value = '';
      newCatParent.value = null;
      showCatForm.value = false;
      loadTree();
      loadAllCats();
    }

    async function delCategory(cid) {
      if (!confirm('确认删除该分类？')) return;
      const body = await api('/api/categories/' + cid + '?scope=file', { method: 'DELETE' });
      if (body.code !== 0) { showToast(body.msg || '删除分类失败', 'error'); return; }
      showToast('分类已删除', 'success');
      if (activeFilter.catId === cid) { activeFilter.catId = null; }
      loadTree();
      loadAllCats();
      loadList();
    }

    // 移动文件到分类
    async function moveFileCategory(item, val) {
      const cid = val === '' ? null : parseInt(val, 10);
      const body = await api('/api/files/' + item.id + '/category', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category_id: cid }),
      });
      if (body.code !== 0) { showToast(body.msg || '移动分类失败', 'error'); return; }
      item.category_id = cid;
      showToast(cid ? '已移动到分类' : '已移出分类', 'success');
      loadTree();
    }

    // ---------- 搜索 ----------
    const searchKw = ref('');
    const searchMode = ref(false);
    const searchLoading = ref(false);
    const searchResults = reactive({ list: [], total: 0 });

    async function doSearch() {
      const q = searchKw.value.trim();
      if (!q) { clearSearch(); return; }
      searchMode.value = true;
      searchLoading.value = true;
      const body = await api('/api/files/search?q=' + encodeURIComponent(q));
      searchLoading.value = false;
      if (body.code !== 0) { showToast(body.msg || '搜索失败', 'error'); return; }
      searchResults.list = body.data.list;
      searchResults.total = body.data.total;
    }

    function clearSearch() {
      searchKw.value = '';
      searchMode.value = false;
      searchResults.list = [];
      searchResults.total = 0;
      loadList();
    }

    // ---------- 上传 ----------
    const fileInput = ref(null);
    const uploading = ref(false);
    const dragOver = ref(false);

    function pickFile() { if (!uploading.value) fileInput.value.click(); }

    function onInputChange(e) {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';
      if (f) doUpload(f);
    }

    function onDrop(e) {
      dragOver.value = false;
      if (uploading.value) return;
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) doUpload(f);
    }

    function preCheck(f) {
      const name = f.name.toLowerCase();
      if (name.endsWith('.csv')) {
        if (f.size > 500 * 1024 * 1024) return 'CSV 文件超过 500MB 上限，请压缩或拆分后再上传';
      } else if (name.endsWith('.xlsx') || name.endsWith('.xls')) {
        if (f.size > 50 * 1024 * 1024) return 'Excel 文件超过 50MB 上限，请压缩或拆分后再上传';
      } else {
        return '不支持的文件类型，仅支持 .xlsx / .xls / .csv';
      }
      return '';
    }

    async function doUpload(f) {
      const err = preCheck(f);
      if (err) { showToast(err, 'error'); return; }
      uploading.value = true;
      const fd = new FormData();
      fd.append('file', f);
      const body = await api('/api/files/upload', { method: 'POST', body: fd });
      uploading.value = false;
      if (body.code !== 0) { showToast(body.msg || '导入失败', 'error'); return; }
      showToast('导入成功：' + fmtNum(body.data.row_count) + ' 行 × ' + body.data.col_count
        + ' 列，耗时 ' + body.data.cost_sec + ' 秒', 'success');
      page.value = 1;
      loadList();
      loadTree();
    }

    // ---------- 删除 ----------
    async function del(item) {
      // 破坏性操作铁律：二次确认；源文件删除会级联清除清洗产物与记录
      const tip = item.parent_file_id == null
        ? '其字段信息、体检/清洗记录将一并删除；若已有清洗产物，产物文件也会一并删除。'
        : '其字段信息与对应清洗记录将一并删除。';
      if (!confirm('确认删除文件「' + item.origin_name + '」？' + tip)) return;
      const body = await api('/api/files/' + item.id, { method: 'DELETE' });
      if (body.code !== 0) { showToast(body.msg || '删除失败，请稍后重试', 'error'); return; }
      const cascaded = body.data && body.data.deleted_files ? body.data.deleted_files - 1 : 0;
      showToast(cascaded > 0
        ? '已删除：' + item.origin_name + '（含 ' + cascaded + ' 个清洗产物）'
        : '已删除：' + item.origin_name, 'success');
      if (list.value.length === 1 && page.value > 1) page.value -= 1;
      loadList();
      loadTree();
    }

    // ---------- 预览 ----------
    const preview = ref(null);
    const previewLoading = ref(false);
    // 预览行硬上限 5 行（后端已截断，前端再兜底一次），防止响应异常时全量渲染卡顿
    const previewRows = computed(() => (preview.value?.preview_rows || []).slice(0, 5));
    const encodingOptions = ['utf-8', 'utf-8-sig', 'gbk', 'gb2312'];
    const encInput = ref('utf-8');
    const newTag = ref('');
    const noteDraft = ref('');

    async function openPreview(id) {
      mode.value = 'preview';
      previewLoading.value = true;
      preview.value = null;
      const body = await api('/api/files/' + id + '/preview');
      previewLoading.value = false;
      if (body.code !== 0) {
        showToast(body.msg || '预览加载失败', 'error');
        mode.value = 'list';
        return;
      }
      preview.value = body.data;
      encInput.value = body.data.encoding || 'utf-8';
      noteDraft.value = body.data.note || '';
    }

    async function applyEncoding() {
      const p = preview.value;
      if (!p) return;
      if (encInput.value === p.encoding) { showToast('编码未变化，无需重新解析', 'info'); return; }
      previewLoading.value = true;
      const body = await api('/api/files/' + p.id + '/encoding', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ encoding: encInput.value }),
      });
      previewLoading.value = false;
      if (body.code !== 0) { showToast(body.msg || '重新解析失败', 'error'); return; }
      showToast('已按 ' + encInput.value + ' 重新解析', 'success');
      openPreview(p.id);
      loadList();
    }

    // 标签增删
    async function addTag() {
      const t = newTag.value.trim();
      if (!t) return;
      const p = preview.value;
      const tags = [...(p.tags || [])];
      if (tags.includes(t)) { showToast('标签已存在', 'info'); return; }
      tags.push(t);
      const body = await api('/api/files/' + p.id + '/tags', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tags }),
      });
      if (body.code !== 0) { showToast(body.msg || '标签更新失败', 'error'); return; }
      p.tags = body.data.tags;
      newTag.value = '';
      showToast('标签已添加', 'success');
    }

    async function removeTag(t) {
      const p = preview.value;
      const tags = (p.tags || []).filter(x => x !== t);
      const body = await api('/api/files/' + p.id + '/tags', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tags }),
      });
      if (body.code !== 0) { showToast(body.msg || '标签更新失败', 'error'); return; }
      p.tags = body.data.tags;
      showToast('标签已移除', 'success');
    }

    // 备注保存
    async function saveNote() {
      const p = preview.value;
      const body = await api('/api/files/' + p.id, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: noteDraft.value }),
      });
      if (body.code !== 0) { showToast(body.msg || '备注保存失败', 'error'); return; }
      p.note = body.data.note;
      showToast('备注已保存', 'success');
    }

    // 字段类型修正
    async function fixFieldType(f, val) {
      const p = preview.value;
      const body = await api('/api/files/' + p.id + '/fields/' + f.id, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ manual_type: val }),
      });
      if (body.code !== 0) { showToast(body.msg || '类型修正失败', 'error'); return; }
      f.manual_type = body.data.manual_type;
      f.display_type = body.data.manual_type || f.inferred_type;
      showToast(val ? '类型已修正' : '已恢复推断类型', 'success');
    }

    function backToList() {
      mode.value = 'list';
      preview.value = null;
      loadList();
      loadTree();
    }

    // ---------- 从数据库取数（卡12） ----------
    const dbModal = reactive({
      show: false, busy: false, cid: null, mode: 'table',
      conns: [], tables: [], loadingTables: false,
      previewTable: '', previewCols: [], previewRows: [],
      sql: '', err: '',
    });

    async function loadDbConns() {
      const body = await api('/api/db/connections');
      if (body.code === 0) dbModal.conns = body.data || [];
      else dbModal.err = body.msg || '连接列表加载失败';
    }

    function openDbModal() {
      dbModal.show = true;
      dbModal.err = '';
      dbModal.sql = '';
      dbModal.previewTable = '';
      dbModal.previewCols = [];
      dbModal.previewRows = [];
      loadDbConns();
    }

    function closeDbModal() {
      dbModal.show = false;
    }

    function gotoSettings() {
      window.location.hash = '#/settings';
    }

    async function onConnChange() {
      dbModal.tables = [];
      dbModal.previewTable = '';
      dbModal.previewCols = [];
      dbModal.previewRows = [];
      dbModal.err = '';
      if (!dbModal.cid) return;
      dbModal.loadingTables = true;
      const body = await api('/api/db/' + dbModal.cid + '/tables');
      dbModal.loadingTables = false;
      if (body.code !== 0) { dbModal.err = body.msg || '表列表加载失败'; return; }
      dbModal.tables = body.data.tables || [];
    }

    async function previewDbTable(table) {
      dbModal.previewTable = table;
      dbModal.previewCols = [];
      dbModal.previewRows = [];
      dbModal.err = '';
      const body = await api('/api/db/' + dbModal.cid + '/preview?table=' + encodeURIComponent(table));
      if (body.code !== 0) { dbModal.err = body.msg || '预览失败'; return; }
      dbModal.previewCols = body.data.columns || [];
      dbModal.previewRows = body.data.rows || [];
    }

    function afterDbImport(body) {
      if (body.code !== 0) { dbModal.err = body.msg || '导入失败'; return; }
      showToast('取数成功：' + fmtNum(body.data.row_count) + ' 行 × ' + body.data.col_count
        + ' 列，耗时 ' + body.data.cost_sec + ' 秒', 'success');
      closeDbModal();
      // 跳转预览新产物
      openPreview(body.data.file_id);
    }

    async function importDbTable() {
      if (!dbModal.previewTable) return;
      dbModal.busy = true; dbModal.err = '';
      const body = await api('/api/db/' + dbModal.cid + '/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sql: 'SELECT * FROM `' + dbModal.previewTable + '`' }),
      });
      dbModal.busy = false;
      afterDbImport(body);
    }

    async function importDbSql() {
      const sql = (dbModal.sql || '').trim();
      if (!sql) { dbModal.err = '请输入 SQL'; return; }
      dbModal.busy = true; dbModal.err = '';
      const body = await api('/api/db/' + dbModal.cid + '/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sql }),
      });
      dbModal.busy = false;
      afterDbImport(body);
    }

    onMounted(() => { loadList(); loadTree(); loadAllCats(); });

    return {
      toast, mode, list, total, page, pageSize, totalPages, loading,
      fileInput, uploading, dragOver,
      preview, previewLoading, previewRows, encodingOptions, encInput, newTag, noteDraft,
      FIELD_TYPES,
      treeView, treeType, treeCustom, typeGroups, cleanedItems, topCats, flatCats,
      showCatForm, newCatName, newCatParent,
      activeFilter,
      searchKw, searchMode, searchLoading, searchResults,
      pickFile, onInputChange, onDrop, goPage, del,
      openPreview, applyEncoding, backToList,
      loadTree, switchTree, selectTypeGroup, selectCategory, selectUncategorized,
      createCategory, delCategory, moveFileCategory,
      doSearch, clearSearch, hl,
      addTag, removeTag, saveNote, fixFieldType,
      fmtNum, fmtSize, typeText, typeBase, typeHint,
      dbModal, openDbModal, closeDbModal, gotoSettings, loadDbConns,
      onConnChange, previewDbTable, importDbTable, importDbSql,
    };
  },
};
})();
