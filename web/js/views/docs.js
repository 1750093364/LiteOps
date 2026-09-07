/* 学习文档库视图（卡07）。
   左侧多级分类树（新建/重命名/删除，删除后文档归未分类）+ 文件列表（名/类型/分类/时间/删除）
   + 多文件上传 + 关键字搜索（命中摘要片段展示）；
   预览：pdf 内嵌 iframe / 图片直显 / md 最简排版（标题/列表/代码块）/ txt 原文 /
   docx·xlsx 展示抽取文本并带 V1.5 降级说明黄条。
   Vue3 全局构建，无构建步骤。整体包在 IIFE 内，避免全局 const 冲突。 */
(function () {
const { ref, reactive, computed, onMounted } = Vue;

const PAGE_SIZE = 20;
const TYPE_LABELS = {
  docx: 'Word', doc: 'Word', pdf: 'PDF', xlsx: 'Excel', xls: 'Excel',
  md: 'Markdown', txt: 'TXT', png: '图片', jpg: '图片', jpeg: '图片',
};

window.DocsView = {
  name: 'DocsView',
  template: `
  <div class="docs-view">
    <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.msg }}</div>

    <div class="view-head">
      <div>
        <h1>学习文档库</h1>
        <p>内部工具教程 / 指标口径 / 复盘存档统一沉淀，支持分类、检索与在线预览</p>
      </div>
      <div class="flex-row">
        <button class="btn" @click="openCatModal(null)">＋ 新建分类</button>
        <button class="btn btn-primary" :disabled="uploading" @click="fileInput.click()">
          {{ uploading ? '上传中…' : '⬆ 上传文档' }}
        </button>
        <input type="file" ref="fileInput" style="display:none" multiple
               accept=".docx,.pdf,.xlsx,.xls,.md,.txt,.png,.jpg,.jpeg" @change="onUpload">
      </div>
    </div>

    <div class="snp-toolbar">
      <input class="search-input snp-search" v-model="kw"
             placeholder="搜索文件名 / 正文摘要，如：指标口径、工具教程…"
             @input="onSearchInput">
    </div>

    <div class="snp-layout">
      <!-- 左侧分类树（多级） -->
      <aside class="snp-tree docs-tree">
        <div class="snp-tree-item" :class="{ active: cat === '' }" @click="selectCat('')">
          <span>全部文档</span><span class="snp-cnt">{{ cats.total }}</span>
        </div>
        <div class="snp-tree-item" :class="{ active: cat === -1 }" @click="selectCat(-1)">
          <span>未分类</span><span class="snp-cnt">{{ cats.uncategorized }}</span>
        </div>
        <div v-for="node in flatCats" :key="node.id" class="snp-tree-item docs-tree-node"
             :class="{ active: cat === node.id }"
             :style="{ paddingLeft: (10 + node.depth * 16) + 'px' }"
             @click="selectCat(node.id)">
          <span class="docs-tree-name" :title="node.name">
            <span v-if="node.children.length" class="docs-tree-arrow">▸</span>{{ node.name }}
          </span>
          <span class="snp-cnt">{{ node.count }}</span>
          <span class="docs-node-ops" @click.stop>
            <button class="docs-op-btn" title="新建子分类" @click="openCatModal(node)">＋</button>
            <button class="docs-op-btn" title="重命名" @click="openCatModal(null, node)">✎</button>
            <button class="docs-op-btn danger" title="删除分类" @click="removeCat(node)">✕</button>
          </span>
        </div>
      </aside>

      <!-- 右侧文件列表 -->
      <div class="snp-main">
        <div v-if="loading" class="empty">加载中…</div>
        <div v-else-if="list.length === 0" class="empty">
          {{ kw.trim() ? '没有匹配的文档，换个关键字试试' : '暂无文档，点击右上角「上传文档」开始沉淀' }}
        </div>
        <div v-else class="docs-list">
          <div v-for="item in list" :key="item.id" class="docs-row">
            <span class="badge" :class="'doc-type-' + item.doc_type">{{ typeLabel(item.doc_type) }}</span>
            <div class="docs-row-main">
              <a class="docs-row-name" href="javascript:void(0)" @click="openPreview(item)"
                 v-html="hl(item.name)"></a>
              <div class="docs-row-sub" v-if="kw.trim() && item.summary_snippet"
                   v-html="hl(item.summary_snippet)"></div>
            </div>
            <span class="docs-row-cat">{{ item.category_name || '未分类' }}</span>
            <span class="docs-row-time">{{ item.created_at }}</span>
            <button class="snp-text-btn danger" @click="removeDoc(item)">删除</button>
          </div>
        </div>

        <div class="snp-pager" v-if="!kw.trim() && total > pageSize">
          <button class="btn btn-mini" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
          <span>第 {{ page }} 页 / 共 {{ totalPages }} 页（{{ total }} 条）</span>
          <button class="btn btn-mini" :disabled="page >= totalPages" @click="goPage(page + 1)">下一页</button>
        </div>
      </div>
    </div>

    <!-- 预览弹窗 -->
    <div class="modal-mask" v-if="pv.show" @click.self="pv.show = false">
      <div class="modal docs-preview-modal">
        <div class="docs-pv-head">
          <span class="badge" :class="'doc-type-' + pv.doc_type">{{ typeLabel(pv.doc_type) }}</span>
          <h3 class="docs-pv-title">{{ pv.name }}</h3>
          <button class="btn btn-mini" @click="pv.show = false">关闭</button>
        </div>
        <div v-if="isOffice" class="docs-notice">
          Office 文件在线渲染为 V1.5 能力，当前为文本提取预览
        </div>
        <div class="docs-pv-body">
          <div v-if="pv.loading" class="empty">加载中…</div>
          <iframe v-else-if="pv.doc_type === 'pdf'" class="docs-frame"
                  :src="'/api/docs/' + pv.id + '/raw'"></iframe>
          <div v-else-if="isImage" class="docs-img-wrap">
            <img :src="'/api/docs/' + pv.id + '/raw'" :alt="pv.name">
          </div>
          <div v-else-if="pv.doc_type === 'md'" class="md-body" v-html="mdHtml"></div>
          <pre v-else class="docs-text">{{ pv.text }}</pre>
        </div>
      </div>
    </div>

    <!-- 新建 / 重命名分类弹窗 -->
    <div class="modal-mask" v-if="catModal.show" @click.self="catModal.show = false">
      <div class="modal">
        <h3>{{ catModal.editId ? '重命名分类' : '新建分类' }}</h3>
        <p class="docs-cat-parent" v-if="catModal.parentName">上级分类：{{ catModal.parentName }}</p>
        <label class="fm-label">分类名称
          <input v-model="catModal.name" placeholder="如：内部工具教程 / 指标口径 / 复盘存档"
                 @keyup.enter="saveCat">
        </label>
        <div class="modal-foot">
          <button class="btn" @click="catModal.show = false">取消</button>
          <button class="btn btn-primary" :disabled="catSaving" @click="saveCat">
            {{ catSaving ? '保存中…' : '保存' }}
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
      toastTimer = setTimeout(() => { toast.show = false; }, 3600);
    }

    async function api(path, opts) {
      const res = await fetch(path, opts);
      try { return await res.json(); }
      catch { return { code: -1, msg: '服务响应异常' }; }
    }

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

    // ---------- 分类树 ----------
    const cats = ref({ categories: [], uncategorized: 0, total: 0 });
    const cat = ref('');   // '' 全部 / -1 未分类 / 分类 id

    // 多级树扁平化（DFS，带深度供缩进）
    const flatCats = computed(() => {
      const out = [];
      (function walk(nodes, depth) {
        nodes.forEach(n => {
          out.push({ ...n, depth });
          if (n.children.length) walk(n.children, depth + 1);
        });
      })(cats.value.categories, 0);
      return out;
    });

    async function loadCats() {
      const body = await api('/api/docs/categories');
      if (body.code === 0) cats.value = body.data;
    }
    function selectCat(c) { cat.value = c; page.value = 1; load(); }

    // ---------- 分类新建 / 重命名 / 删除 ----------
    const catModal = reactive({ show: false, editId: null, parentId: null, parentName: '', name: '' });
    const catSaving = ref(false);

    function openCatModal(parentNode, editNode) {
      if (editNode) {
        Object.assign(catModal, { show: true, editId: editNode.id, parentId: null,
                                  parentName: '', name: editNode.name });
      } else {
        Object.assign(catModal, { show: true, editId: null,
                                  parentId: parentNode ? parentNode.id : null,
                                  parentName: parentNode ? parentNode.name : '', name: '' });
      }
    }
    async function saveCat() {
      const name = catModal.name.trim();
      if (!name) { showToast('请填写分类名称', 'error'); return; }
      catSaving.value = true;
      let body;
      if (catModal.editId) {
        body = await api('/api/docs/categories/' + catModal.editId, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
      } else {
        body = await api('/api/docs/categories', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, parent_id: catModal.parentId }),
        });
      }
      catSaving.value = false;
      if (body.code !== 0) { showToast(body.msg || '保存失败', 'error'); return; }
      catModal.show = false;
      showToast(catModal.editId ? '分类已重命名' : '分类已创建', 'success');
      loadCats();
    }
    async function removeCat(node) {
      if (!window.confirm('确定删除分类「' + node.name + '」？' +
          (node.children.length ? '其子分类将一并删除，' : '') +
          '分类下的文档将归入未分类')) return;
      const body = await api('/api/docs/categories/' + node.id, { method: 'DELETE' });
      if (body.code !== 0) { showToast(body.msg || '删除失败', 'error'); return; }
      if (cat.value === node.id) cat.value = '';
      showToast('分类已删除', 'success');
      loadCats();
      load();
    }

    // ---------- 列表 / 搜索 ----------
    const kw = ref('');
    const list = ref([]);
    const total = ref(0);
    const page = ref(1);
    const pageSize = PAGE_SIZE;
    const loading = ref(false);
    const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

    let searchTimer = null;
    function onSearchInput() {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { page.value = 1; load(); }, 300);
    }
    function goPage(p) { page.value = p; load(); }

    async function load() {
      loading.value = true;
      const k = kw.value.trim();
      let body;
      if (k) {
        body = await api('/api/docs/search?q=' + encodeURIComponent(k));
      } else {
        const params = new URLSearchParams();
        params.set('page', page.value);
        params.set('page_size', pageSize);
        if (cat.value !== '') params.set('category_id', cat.value);
        body = await api('/api/docs?' + params.toString());
      }
      loading.value = false;
      if (body.code !== 0) { showToast(body.msg || '文档列表加载失败', 'error'); return; }
      list.value = body.data.list;
      total.value = body.data.total;
    }

    function typeLabel(t) { return TYPE_LABELS[t] || t; }
    const isImage = computed(() => ['png', 'jpg', 'jpeg'].includes(pv.doc_type));
    const isOffice = computed(() => ['docx', 'doc', 'xlsx', 'xls'].includes(pv.doc_type));

    // ---------- Markdown 最简渲染（标题/列表/代码块/粗体/行内代码） ----------
    const mdHtml = ref('');
    function mdInline(s) {
      return s
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/`([^`]+)`/g, '<code>$1</code>');
    }
    function renderMd(src) {
      const lines = String(src || '').split(/\r?\n/);
      let html = '', inCode = false, codeBuf = [], listTag = null;
      const closeList = () => {
        if (listTag) { html += '</' + listTag + '>'; listTag = null; }
      };
      const flushCode = () => {
        if (inCode) {
          html += '<pre><code>' + escHtml(codeBuf.join('\n')) + '</code></pre>';
          codeBuf = []; inCode = false;
        }
      };
      for (const line of lines) {
        if (/^```/.test(line.trim())) {
          if (inCode) { flushCode(); } else { closeList(); inCode = true; }
          continue;
        }
        if (inCode) { codeBuf.push(line); continue; }
        const h = line.match(/^(#{1,6})\s+(.*)$/);
        if (h) {
          closeList();
          const l = h[1].length;
          html += '<h' + l + '>' + mdInline(escHtml(h[2])) + '</h' + l + '>';
          continue;
        }
        const ul = line.match(/^\s*[-*+]\s+(.*)$/);
        const ol = line.match(/^\s*\d+[.、)]\s+(.*)$/);
        if (ul) {
          if (listTag !== 'ul') { closeList(); html += '<ul>'; listTag = 'ul'; }
          html += '<li>' + mdInline(escHtml(ul[1])) + '</li>';
          continue;
        }
        if (ol) {
          if (listTag !== 'ol') { closeList(); html += '<ol>'; listTag = 'ol'; }
          html += '<li>' + mdInline(escHtml(ol[1])) + '</li>';
          continue;
        }
        if (!line.trim()) { closeList(); continue; }
        closeList();
        html += '<p>' + mdInline(escHtml(line)) + '</p>';
      }
      flushCode();
      closeList();
      return html;
    }

    // ---------- 预览 ----------
    const pv = reactive({ show: false, id: null, name: '', doc_type: '', text: '', loading: false });

    async function openPreview(item) {
      Object.assign(pv, { show: true, id: item.id, name: item.name,
                          doc_type: item.doc_type, text: '', loading: true });
      mdHtml.value = '';
      try {
        if (item.doc_type === 'md' || item.doc_type === 'txt') {
          const res = await fetch('/api/docs/' + item.id + '/raw');
          const text = await res.text();
          if (item.doc_type === 'md') mdHtml.value = renderMd(text);
          else pv.text = text;
        } else if (isOffice.value) {
          const body = await api('/api/docs/' + item.id + '/preview');
          if (body.code !== 0) { showToast(body.msg || '预览加载失败', 'error'); }
          else pv.text = body.data.summary || '（未抽取到文本内容）';
        }
        // pdf / 图片：iframe / img 直接指向 /raw，无需额外请求
      } catch (e) {
        showToast('预览内容加载失败', 'error');
      }
      pv.loading = false;
    }

    // ---------- 上传 ----------
    const fileInput = ref(null);
    const uploading = ref(false);

    async function onUpload(e) {
      const files = Array.from(e.target.files || []);
      e.target.value = '';
      if (!files.length) return;
      uploading.value = true;
      let okCnt = 0;
      const errs = [];
      for (const f of files) {
        const fd = new FormData();
        fd.append('file', f);
        try {
          const res = await fetch('/api/docs/upload', { method: 'POST', body: fd });
          const body = await res.json();
          if (body.code === 0) okCnt++;
          else errs.push(f.name + '：' + (body.msg || '上传失败'));
        } catch (err) {
          errs.push(f.name + '：上传异常');
        }
      }
      uploading.value = false;
      if (okCnt && errs.length) showToast('成功上传 ' + okCnt + ' 个；失败：' + errs.join('；'), 'error');
      else if (okCnt) showToast('成功上传 ' + okCnt + ' 个文档', 'success');
      else showToast(errs.join('；') || '上传失败', 'error');
      load();
      loadCats();
    }

    // ---------- 删除 ----------
    async function removeDoc(item) {
      if (!window.confirm('确定删除文档《' + item.name + '》？物理文件将同步删除，此操作不可恢复')) return;
      const body = await api('/api/docs/' + item.id, { method: 'DELETE' });
      if (body.code !== 0) { showToast(body.msg || '删除失败', 'error'); return; }
      showToast('文档已删除', 'success');
      load();
      loadCats();
    }

    onMounted(() => { load(); loadCats(); });

    return {
      toast, kw, cat, cats, flatCats, list, total, page, pageSize, loading, totalPages,
      catModal, catSaving, pv, mdHtml, isImage, isOffice, fileInput, uploading,
      onSearchInput, selectCat, goPage, hl, typeLabel, openCatModal, saveCat, removeCat,
      openPreview, onUpload, removeDoc,
    };
  },
};
})();
