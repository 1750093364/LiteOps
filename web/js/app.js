/* 轻析 LiteOps 前端入口：Vue 3 全局构建（本地 vendor 离线加载），无构建步骤。
   导航：URL hash 为唯一真相源（#/files、#/cleaning …），不引入 vue-router。
   - 菜单点击 / 跨视图跳转只写 location.hash；
   - hashchange（含浏览器前进/后退）与页面初始化统一从 hash 解析视图；
   - 无 hash 默认首页（files）；?framework={id} 深链直达需求框架确认页（replace 写 hash，不增历史）。 */
const { createApp, ref } = Vue;

createApp({
  components: {
    // 视图组件（web/js/views/*.js 挂在 window 上，需先于本文件加载）
    'files-view': window.FilesView,
    'cleaning-view': window.CleaningView,
    'snippets-view': window.SnippetsView,
    'docs-view': window.DocsView,
    'tickets-view': window.TicketsView,
    'settings-view': window.SettingsView,
    'reports-view': window.ReportsView,
  },
  setup() {
    const menus = [
      { key: 'files', label: '数据存储' },
      { key: 'cleaning', label: '数据清洗' },
      { key: 'snippets', label: 'SQL片段库' },
      { key: 'docs', label: '文档库' },
      { key: 'tickets', label: '需求清单' },
      { key: 'reports', label: '报告工作台' },
      { key: 'settings', label: '设置' },
    ];
    const VALID_KEYS = new Set(menus.map(m => m.key));

    // 演示模式：从 /api/health 读取 demo_mode，控制顶栏黄色横幅
    const demoMode = ref(false);
    async function loadDemoMode() {
      try {
        const res = await fetch('/api/health');
        const body = await res.json();
        demoMode.value = !!(body && body.data && body.data.demo_mode);
      } catch { demoMode.value = false; }
    }
    loadDemoMode();

    // 从 location.hash 解析视图键：#/reports → 'reports'；无/非法 hash → null
    function viewFromHash() {
      const seg = (window.location.hash || '').replace(/^#\/?/, '').split('/')[0].trim();
      return VALID_KEYS.has(seg) ? seg : null;
    }

    // 深链：?framework={id} 直达需求框架确认页（仅首次加载、且 hash 未指定视图时生效）
    const qs = new URLSearchParams(window.location.search);
    if (!viewFromHash() && qs.get('framework')) {
      // replaceState：不产生多余历史记录，query string 保留供 tickets 视图读取
      window.history.replaceState(null, '', '#/tickets');
    }

    const currentView = ref(viewFromHash() || 'files');

    // hashchange：浏览器前进/后退、菜单点击写 hash 后均走这里，保证视图与 URL 永远一致
    function applyHash() {
      currentView.value = viewFromHash() || 'files';
    }
    window.addEventListener('hashchange', applyHash);

    // 菜单点击 / 跨视图跳转：只写 hash，由 hashchange 统一切换视图
    function goto(key) {
      if (!VALID_KEYS.has(key) || viewFromHash() === key) return;
      window.location.hash = '#/' + key;
    }
    window.addEventListener('liteops:goto', (e) => {
      goto(e.detail);  // 清洗记录 → 数据存储查看产物等跨视图跳转
    });

    return { menus, currentView, goto, demoMode };
  },
}).mount('#app');
