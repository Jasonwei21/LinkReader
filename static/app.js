/* 链接抓取摘要器 · 前端
   Vue 3 + Arco Design Vue（本地 vendor，离线可用）
   布局：顶栏 / 左侧任务栏 / 中间工作区 / 右侧信息栏
   配色：Claude 暖色暗调，主题变量在 style.css 顶部
*/
(function () {
  'use strict';

  const { createApp, ref, reactive, computed, onMounted, nextTick } = Vue;

  /* 版本号只在这里写一次，设置弹窗和指南页脚都引用它 */
  const VER = '1.1.0-beta';

  /* ------------------------------------------------------------ 工具 */
  const URL_RE = /https?:\/\/[^\s<>"'`）)，,。；;、]+/gi;
  const TRIM_RE = /[.,;:!?)\]】>]+$/;

  function extractUrls(text) {
    const out = [];
    if (!text) return out;
    let m;
    URL_RE.lastIndex = 0;
    while ((m = URL_RE.exec(text)) !== null) {
      const u = m[0].replace(TRIM_RE, '');
      if (u.length > 10 && out.indexOf(u) < 0) out.push(u);
    }
    return out;
  }

  async function api(path, opt) {
    const r = await fetch(path, opt);
    let data = {};
    try { data = await r.json(); } catch (e) { /* 非 JSON */ }
    if (!r.ok) throw new Error(data.error || ('请求失败 HTTP ' + r.status));
    return data;
  }

  const JSON_HEAD = { 'Content-Type': 'application/json' };
  const BUSY_PREFIX = ['抓取中', '已抓取', '生成摘要', '翻译中', '写入文件'];

  function pillOf(it) {
    const s = it.status || '';
    if (s === '完成' || s === '已抓取') {
      if (it.needs_review) return { cls: 'review', text: 'OCR 待校' };
      if (it.via === 'render') return { cls: 'ok', text: '渲染取得' };
      return { cls: 'ok', text: '完成' };
    }
    if (s === '排队中') return { cls: 'queued', text: '排队' };
    for (let i = 0; i < BUSY_PREFIX.length; i++) {
      if (s.indexOf(BUSY_PREFIX[i]) === 0) return { cls: 'busy', text: s };
    }
    // 失败类只给分类词，细节交给跟在后面的 reason，避免两句话说同一件事
    if (s.indexOf('墙') >= 0) return { cls: 'skip', text: '订阅墙' };
    if (/拦截|403|429/.test(s)) return { cls: 'skip', text: '反爬拦截' };
    if (/过短|非文章|无法识别/.test(s)) return { cls: 'skip', text: '无正文' };
    if (/失败|超时|连不上/.test(s)) return { cls: 'skip', text: '未收录' };
    return { cls: 'bad', text: s || '异常' };
  }

  /* reason 常以状态词开头（"订阅墙：正文不可完整读取"），前面已经有标签了，
     这里把重复的前缀剪掉 */
  function detailOf(it) {
    let r = (it.reason || '').trim();
    if (!r || r === '公开可读') return '';
    // 去掉与左侧标签重复的前缀（"未收录：" / "抓取失败："），标签已经把话说了一半
    const tags = [pillOf(it).text, '抓取失败', '失败'];
    for (let i = 0; i < tags.length; i++) {
      const t = tags[i];
      if (t && r.indexOf(t) === 0) {
        r = r.slice(t.length).replace(/^[：:，,、\s]+/, '');
      }
    }
    return r;
  }

  function shortUrl(u) {
    const s = String(u || '').replace(/^https?:\/\//, '').replace(/^www\./, '');
    return s.length > 46 ? s.slice(0, 45) + '…' : s;
  }

  function trim(s, n) {
    s = String(s || '');
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  const ICON = {
    spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M3 12h3M18 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/><circle cx="12" cy="12" r="3.2"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.4"/><path d="M12 7.6V12l3 1.9"/></svg>',
    gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.1"/><path d="M19.1 14.5a1.7 1.7 0 0 0 .4 1.9l.1.1a2 2 0 1 1-2.9 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.9l.1-.1a1.7 1.7 0 0 0-1.2-2.9H2.6a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.9-2.8l.1.1a1.7 1.7 0 0 0 1.9.4h.1a1.7 1.7 0 0 0 1-1.5V2.6a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.9l-.1.1a1.7 1.7 0 0 0-.4 1.9v.1a1.7 1.7 0 0 0 1.5 1h.2a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1z"/></svg>',
    doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4 4"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
    folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.4h7A1.5 1.5 0 0 1 19 9.9v7.6A1.5 1.5 0 0 1 17.5 19h-13A1.5 1.5 0 0 1 3 17.5z"/></svg>',
    chart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19V5M4 19h16"/><path d="M8 15v-4M12 15V8M16 15v-6"/></svg>',
    target: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.4"/></svg>',
    down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="m7.5 11 4.5 4.5 4.5-4.5"/><path d="M5 20h14"/></svg>',
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2.6v2.2M12 19.2v2.2M4.4 4.4l1.6 1.6M18 18l1.6 1.6M2.6 12h2.2M19.2 12h2.2M4.4 19.6 6 18M18 6l1.6-1.6"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 14.3A8.6 8.6 0 0 1 9.7 3.5a8.6 8.6 0 1 0 10.8 10.8z"/></svg>',
    sheet: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.4" y="4.4" width="17.2" height="15.2" rx="2.2"/><path d="M3.4 9.4h17.2M9.2 9.4v10.2M15 9.4v10.2"/></svg>',
    book: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.2c2.6-1 5.3-1 8 .8v13c-2.7-1.8-5.4-1.8-8-.8z"/><path d="M20 5.2c-2.6-1-5.3-1-8 .8v13c2.7-1.8 5.4-1.8 8-.8z"/><path d="M7 8.6c.8-.2 1.6-.2 2.4.1M7 11.6c.8-.2 1.6-.2 2.4.1M14.6 8.7c.8-.3 1.6-.3 2.4-.1M14.6 11.7c.8-.3 1.6-.3 2.4-.1"/></svg>',
  };

  /* ------------------------------------------------------------ 模板 */
  const TEMPLATE = `
<div class="shell">

  <!-- ===================================================== 顶栏 -->
  <header class="topbar">
    <div class="brand">
      <img src="/static/icon-mark.svg" alt="">
      <b>LinkReader</b>
    </div>
    <span class="tb-sep"></span>
    <span class="crumb">{{ crumb }}</span>
    <span class="chip" v-if="job">{{ counts.all }} 条</span>
    <span class="chip mono" v-if="job">{{ job.id }}</span>
    <span class="chip" :class="stateCls" v-if="job">{{ stateText }}</span>

    <div class="tb-right">
      <div class="tb-search" v-if="job && view === 'work'">
        <i v-html="icon.search"></i>
        <input v-model="q" placeholder="搜索标题或链接" spellcheck="false">
      </div>
      <button class="ibtn" :class="{live: guideOpen}" @click="openGuide"
              title="使用指南" v-html="icon.book"></button>
      <button class="ibtn" :class="{live: llmReady}" @click="openCfg"
              :title="llmReady ? '大模型已启用' : '未配置大模型，摘要走本地提取式'"
              v-html="icon.gear"></button>
    </div>
  </header>

  <div class="body-row">

    <!-- =================================================== 左栏 -->
    <aside class="side">
      <div class="side-top">
        <button class="newtask" @click="newTask" :disabled="running">
          <i v-html="icon.plus" style="display:flex"></i>新建任务
        </button>
      </div>

      <nav class="side-nav">
        <button class="nav-row" :class="{on: view === 'work'}" @click="go('work')">
          <i v-html="icon.spark"></i><span>工作台</span>
          <i class="n" v-if="urls.length">{{ urls.length }}</i>
        </button>
        <button class="nav-row" :class="{on: view === 'history'}" @click="go('history')">
          <i v-html="icon.clock"></i><span>历史任务</span>
          <i class="n" v-if="history.length">{{ history.length }}</i>
        </button>
      </nav>

      <div class="side-label">任务</div>
      <div class="side-scroll">
        <template v-for="g in histGroups" :key="g.name">
          <div class="grp"><i v-html="icon.folder" style="display:flex"></i>{{ g.name }}</div>
          <button class="task" v-for="h in g.items" :key="h.id"
                  :class="{on: job && job.id === h.id}" @click="openHist(h.id)">
            <i class="dot" :class="h.done ? 'ok' : 'skip'"></i>
            <span class="t">{{ h.first || h.id }}</span>
            <span class="time">{{ relTime(h.time) }}</span>
          </button>
        </template>
        <div class="side-empty" v-if="!history.length">
          还没有任务。把链接贴进工作台，跑一次就出现在这里。
        </div>
      </div>

      <div class="side-foot">
        <div class="foot-theme" :class="themeName === '夜间' ? 'dark' : 'light'"
             title="外观跟随系统设置，改系统主题会自动切换">
          <i v-html="themeName === '夜间' ? icon.moon : icon.sun" style="display:flex"></i>
          <span>跟随系统 · {{ themeName }}</span>
        </div>
        正文转录自原文，版权归原作者与机构所有。<br>
        <b>付费墙内容不做任何绕过。</b>
      </div>
    </aside>

    <!-- =================================================== 中栏 -->
    <main class="main">
      <div class="main-scroll">
        <div class="inner">

          <!-- -------------------------------------------- 工作台 -->
          <template v-if="view === 'work'">

            <!-- 没任务：说明 + 输入 -->
            <template v-if="!job">
              <div class="hero">
                <h1>抓一批链接，拿到能用的语料</h1>
                <p>自动抽取正文、剔除订阅墙与栏目页，生成摘要，可选译成中文，
                  导出纯文本、Markdown 与 Word。不配大模型也能跑，摘要走本地提取式。</p>
              </div>

              <section class="composer" :class="{hot: dragOver}"
                       @dragover.prevent="onDropOver" @dragleave="dragOver = false"
                       @drop.prevent="onDrop">
                <textarea v-model="raw" spellcheck="false" wrap="soft"
                  placeholder="https://www.bruegel.org/analysis/...
https://carnegieendowment.org/research/...
https://dialogue.earth/en/digest/...

表格（.xlsx / .csv）也可以直接拖进来，自动找出里面的链接，一行一条"></textarea>
                <div class="composer-bar">
                  <button class="imp-btn" :disabled="importing" @click="pickFile"
                          :title="'读 .xlsx / .csv / .tsv / .txt 里的链接，一行一条'">
                    <i v-html="icon.sheet"></i>
                    <span>{{ importing ? '读取中…' : '导入表格' }}</span>
                  </button>
                  <input ref="fileInput" type="file" class="imp-file" @change="onPick"
                         accept=".xlsx,.xlsm,.xls,.csv,.tsv,.txt,.md">
                  <div class="cnt"><b>{{ urls.length }}</b> 条链接</div>
                  <div class="opts">
                    <a-checkbox v-model="strict">严格模式</a-checkbox>
                    <a-checkbox v-model="pdf">PDF 兜底</a-checkbox>
                    <a-checkbox v-model="renderFB" title="反爬拦截或超时时，用本机浏览器重新打开页面取正文">渲染兜底</a-checkbox>
                    <a-checkbox v-model="ocrFB" title="渲染后仍无正文时，截图后调系统 OCR（有损，需校对）">OCR 兜底</a-checkbox>
                    <a-checkbox v-model="doSum">摘要</a-checkbox>
                    <a-select v-model="trans" size="small" :popup-container="'body'">
                      <a-option value="off">不翻译</a-option>
                      <a-option value="zh">译成中文</a-option>
                      <a-option value="both">中英对照</a-option>
                    </a-select>
                  </div>
                  <a-button class="go" type="primary" :disabled="!urls.length" @click="start">开始抓取</a-button>
                </div>
                <p class="imp-note" v-if="importNote">
                  <i v-html="icon.sheet"></i><span>{{ importNote }}</span>
                </p>
                <p class="warn" v-if="trans !== 'off' && !cfg.llm.has_key">
                  翻译要靠大模型。先在设置里填接口地址和 Key，否则任务会被拦下。
                </p>
              </section>

              <div class="empty">
                <div class="art">
                  <svg viewBox="0 0 190 132" fill="none" stroke-width="1.5"
                       stroke-linecap="round" stroke-linejoin="round">
                    <rect class="p1" x="10" y="16" width="58" height="76" rx="5"/>
                    <path class="l1" d="M23 31h32M23 42h32M23 53h21M23 64h26"/>
                    <rect class="p2" x="34" y="34" width="58" height="76" rx="5"/>
                    <path class="l2" d="M47 49h32M47 60h32M47 71h20M47 82h26"/>
                    <path class="hl" d="M103 73h17"/>
                    <path class="hl" d="m115 67 6 6-6 6"/>
                    <rect class="p3" x="131" y="48" width="46" height="52" rx="5"/>
                    <path class="l3" d="M142 63h24M142 73h24M142 83h13"/>
                  </svg>
                </div>
                <div class="et">
                  <h4>三步拿到可用的语料</h4>
                  <ol>
                    <li>把链接贴进输入框，整段文字也行；Excel / CSV 直接拖进来</li>
                    <li>点「开始抓取」，订阅墙和栏目页会被自动剔除</li>
                    <li>在中间的条目里看摘要，右侧看进度，按需下载 txt / Word</li>
                  </ol>
                </div>
              </div>
            </template>

            <!-- 有任务：筛选 + 条目流 -->
            <template v-else>
              <div class="feed-head">
                <div class="tabs">
                  <button class="tab" v-for="f in filters" :key="f.k"
                          :class="{on: filter === f.k}" @click="filter = f.k">
                    {{ f.n }}<i>{{ f.c }}</i>
                  </button>
                </div>
                <div class="grow"></div>
                <span class="feed-hint">点条目展开摘要与下载</span>
              </div>

              <div class="pline" v-if="running">
                <a-progress :percent="pct" :show-text="false" size="small" />
                <span class="pct">{{ job.done || 0 }}/{{ job.total }}</span>
              </div>

              <article class="card" v-for="it in shownItems" :key="it.idx"
                       :class="{open: sel === it.idx}" @click="sel = it.idx">                <div class="c-head">
                  <span class="c-idx">{{ pad(it.idx) }}</span>
                  <span class="c-title" :class="{faint: !isOk(it)}">{{ it.title || shortUrl(it.url) }}</span>
                  <span class="pill" :class="pillOf(it).cls">{{ pillOf(it).text }}</span>
                </div>

                <div class="c-meta">
                  <span class="url">{{ shortUrl(it.url) }}</span>
                  <template v-if="it.date"><i class="sp"></i><span>{{ it.date }}</span></template>
                  <template v-if="it.chars"><i class="sp"></i>
                    <span>{{ it.chars.toLocaleString() }} 字符</span></template>
                  <template v-if="it.translated"><i class="sp"></i><span>已译</span></template>
                  <template v-if="it.site && !sameHost(it)"><i class="sp"></i>
                    <span>{{ it.site }}</span></template>
                  <span class="why-inline" v-if="!isOk(it) && detailOf(it) && sel !== it.idx"
                        :title="detailOf(it)">{{ trim60(detailOf(it)) }}</span>
                </div>

                <div class="c-body" v-if="sel === it.idx">
                  <template v-if="it.summary">
                    <div class="sum-lab">摘要</div>
                    <p class="sum-text">{{ it.summary }}</p>
                    <div class="kws" v-if="it.keywords && it.keywords.length">
                      <span class="kw" v-for="k in it.keywords" :key="k">{{ k }}</span>
                    </div>
                  </template>
                  <p class="why" v-else-if="detailOf(it)">{{ detailOf(it) }}</p>
                  <p class="why" v-else-if="pillOf(it).cls === 'busy'">正在处理，稍等这个条目跑完。</p>

                  <div class="c-foot">
                    <a-button v-if="it.file" size="small" @click.stop="openFile(it, 'txt')">纯文本 .txt</a-button>
                    <a-button v-if="it.docx_file" size="small" @click.stop="openFile(it, 'docx')">Word .docx</a-button>
                    <a-button v-if="it.file" size="small" @click.stop="openFile(it, 'md')">Markdown .md</a-button>
                    <a class="src" :href="it.url" target="_blank" rel="noreferrer"
                       @click.stop>打开原文 ↗</a>
                  </div>

                  <p class="note" v-if="it.summary_how">摘要方式：{{ it.summary_how }}</p>
                </div>
              </article>

              <div class="side-empty" v-if="!shownItems.length">
                这个筛选下没有条目。
              </div>

              <div class="feed-foot" v-if="job && !running">
                <i class="ff-ico" v-html="icon.down"></i>
                <span class="ff-t"><b>{{ counts.ok }}</b> 篇已收录 · <b>{{ counts.chars }}</b> 字符
                  <span class="ff-dim" v-if="counts.skip">· 剔除 {{ counts.skip }} 条</span></span>
                <a-button size="small" v-if="job.zip" @click="downloadZip">下载全部 zip</a-button>
              </div>
            </template>
          </template>

          <!-- -------------------------------------------- 历史任务 -->
          <template v-else>
            <div class="hero">
              <h1>历史任务</h1>
              <p>每次抓取的结果都在本机 <code>out/</code> 目录下，按开始时间命名。
                点左栏任意一次任务，就能把那批条目重新载入工作台，继续看摘要或下载文件。</p>
            </div>

            <section class="box">
              <h3>输出目录结构</h3>
              <ul class="tree">
                <li><code>out/</code><span>所有任务的总目录</span></li>
                <li><code>  20261119-091224/</code><span>一次任务，按开始时间命名</span></li>
                <li><code>    txt/</code><span>每篇一个纯文本</span></li>
                <li><code>    md/</code><span>带摘要的 Markdown</span></li>
                <li><code>    docx/</code><span>带摘要与译文的 Word</span></li>
                <li><code>    摘要汇总.xlsx</code><span>一次性看全部</span></li>
                <li><code>    结果打包.zip</code><span>整个目录打包</span></li>
                <li><code>    job.json</code><span>结构化结果，历史记录靠它还原</span></li>
              </ul>
            </section>

            <section class="box" v-if="history.length">
              <h3>最近任务</h3>
              <button class="hrow" v-for="h in history" :key="h.id" @click="openHist(h.id)">
                <span class="hid">{{ h.id }}</span>
                <span class="hfirst">{{ h.first || '（没有收录条目）' }}</span>
                <span class="hmeta">{{ h.total }} 条 · 收录 {{ h.done }}</span>
                <span class="hgo">打开 →</span>
              </button>
            </section>

            <section class="box">
              <h3>磁盘占用</h3>
              <p class="tip" style="margin-bottom:0">
                <code>out/_cache/</code> 是页面缓存，随时可以删，不影响已生成的文件。
                历史列表只读 <code>job.json</code>，不加载正文，任务再多也不会卡。
              </p>
            </section>
          </template>

        </div>
      </div>
    </main>

    <!-- =================================================== 右栏 -->
    <aside class="rail">
      <section class="r-sec">
        <div class="r-head">
          <i v-html="icon.chart" style="display:flex"></i>概览
          <span class="sub" v-if="job">{{ job.total }} 条</span>
          <span class="sub" v-else>等待开始</span>
        </div>
        <div class="r-row"><span>已收录</span>
          <b :class="counts.ok ? 'up' : 'dim'">{{ counts.ok ? '+' + counts.ok : '0' }}</b></div>
        <div class="r-row"><span>已剔除</span>
          <b :class="counts.skip ? 'down' : 'dim'">{{ counts.skip ? '-' + counts.skip : '0' }}</b></div>
        <div class="r-row" v-if="counts.busy"><span>处理中</span>
          <b class="run">{{ counts.busy }}</b></div>
        <div class="r-row"><span>正文字符</span><b>{{ job ? counts.chars : '—' }}</b></div>
        <div class="r-row"><span>用时</span><b>{{ job ? (job.elapsed || 0) + 's' : '—' }}</b></div>
      </section>

      <section class="r-sec">
        <div class="r-head">
          <i v-html="icon.target" style="display:flex"></i>本次配置
          <span class="sub ok">就绪</span>
        </div>
        <div class="r-check" v-for="c in cfgChecks" :key="c.t"
             :class="{off: !c.on}">
          <i>{{ c.on ? '✓' : '·' }}</i><span>{{ c.t }}</span>
        </div>
        <div class="r-row" style="margin-top:6px">
          <span>并发 / 超时 / 最短</span>
          <b>{{ cfg.opt.workers }} / {{ cfg.opt.timeout }}s / {{ cfg.opt.min_chars }}</b>
        </div>
      </section>

      <section class="r-sec">
        <div class="r-head">
          <i v-html="icon.doc" style="display:flex"></i>进度
          <span class="sub" :class="{run: running}">{{ counts.ok }}/{{ counts.all }}</span>
        </div>
        <div class="r-item" v-for="it in progressItems" :key="it.idx">
          <i :class="it.cls">{{ it.mark }}</i>
          <span class="t">{{ it.title }}</span>
        </div>
        <div class="rail-empty" v-if="!progressItems.length">
          还没有开始。跑一次之后，每条链接的处理结果会逐条列在这里。
        </div>
      </section>
    </aside>

  </div>
</div>

<!-- ===================================================== 设置 -->
<a-modal v-model:visible="cfgOpen" title="设置" :width="568" class="cfg-modal"
         ok-text="保存" cancel-text="关闭" :on-before-ok="saveCfg" :mask-closable="true">
  <div class="cfg-sec">
    <h3>大模型接口 <a-switch v-model="cfg.llm.enabled" size="small" /></h3>
    <p class="tip">接口地址填到 <code>/v1</code> 为止，程序会自己拼 <code>/chat/completions</code>。
      Key 只写在本机 <code>config.json</code>，不上传任何地方。不填也能跑，摘要退回本地提取式。</p>

    <label class="fl">接口地址 base_url
      <a-input v-model="cfg.llm.base_url" placeholder="https://api.openai.com/v1" allow-clear />
    </label>
    <label class="fl">模型名
      <a-input v-model="cfg.llm.model" placeholder="gpt-4o-mini / deepseek-chat / qwen-plus" allow-clear />
    </label>
    <label class="fl">API Key
      <a-input-password v-model="keyInput" :placeholder="keyPlaceholder" allow-clear />
    </label>

    <div class="srow">
      <a-button size="small" :loading="testing" @click="testLLM">测试接口</a-button>
      <span class="tres" :class="testCls">{{ testMsg }}</span>
    </div>
  </div>

  <div class="cfg-sec">
    <h3>抓取参数</h3>
    <p class="tip">低于「最短字符」的页面算没抓到正文，直接跳过。并发数按站点限流，太高容易被拦。</p>
    <div class="grid3">
      <label class="fl">并发数
        <a-input-number v-model="cfg.opt.workers" :min="1" :max="6" />
      </label>
      <label class="fl">超时（秒）
        <a-input-number v-model="cfg.opt.timeout" :min="10" :max="180" />
      </label>
      <label class="fl">最短字符
        <a-input-number v-model="cfg.opt.min_chars" :min="0" :max="5000" />
      </label>
    </div>
  </div>

  <div class="cfg-sec" style="margin-bottom:4px">
    <h3>关于</h3>
    <ul class="about-list">
      <li><b>版本</b>LinkReader {{ ver }} · 本地运行，除抓取目标站点外无外发请求</li>
      <li><b>指南</b>顶栏「使用指南」带目录，讲完所有开关和状态含义</li>
      <li><b>抓取</b>四类订阅墙判定（metadata / 文案 / 重定向 / 截断），403 单列为反爬</li>
      <li><b>摘要</b>大模型优先，失败自动退回本地提取式并注明原因</li>
      <li><b>翻译</b>按段切分、等长回填，段落一一对齐，不合并</li>
      <li><b>隐私</b>所有结果只写在本机 <code>out/</code>，没有任何上传</li>
      <li><b>外观</b>跟随系统日间 / 夜间设置，当前为{{ themeName }}</li>
    </ul>
  </div>
</a-modal>

<!-- ===================================================== 使用指南 -->
<a-modal v-model:visible="guideOpen" :width="1024" class="guide-modal" :footer="false"
         :mask-closable="true" title="使用指南">
  <div class="gd-wrap">
    <aside class="gd-toc">
      <div class="gd-toc-h">目录</div>
      <div class="gd-toc-list">
        <button v-for="t in guideToc" :key="t.id" class="gd-toc-row"
                :class="{lv3: t.lv === 3, on: t.id === guideActive}"
                @click="jumpGuide(t.id)">
          <span>{{ t.text }}</span>
        </button>
      </div>
      <div class="gd-toc-foot">LinkReader {{ ver }}</div>
    </aside>
    <div class="gd-body" ref="guideBody" @scroll="onGuideScroll">
      <div class="gd-doc" v-html="guideHtml"></div>
      <div class="gd-loading" v-if="!guideHtml">正在读指南…</div>
    </div>
  </div>
</a-modal>

<transition name="fade">
  <div class="toast" :class="toast.cls" v-if="toast.show">
    <i class="bar"></i><span>{{ toast.msg }}</span>
  </div>
</transition>

<!-- 拖文件到窗口任意位置都接住，不接的话浏览器会直接把页面换成那个文件 -->
<transition name="dzfade">
  <div class="dz" v-if="dragging">
    <div class="dz-box">
      <i v-html="icon.sheet"></i>
      <b>松手，把表里的链接读进来</b>
      <span>.xlsx / .csv / .tsv / .txt · 自动去重，一行一条</span>
    </div>
  </div>
</transition>
`;

  /* ------------------------------------------------------------ 应用 */
  const App = {
    template: TEMPLATE,
    setup() {
      const view = ref('work');       // work | history
      const filter = ref('all');      // all | ok | skip
      const q = ref('');
      const cfgOpen = ref(false);

      // 拖表格进来读链接
      const fileInput = ref(null);
      const importing = ref(false);
      const dragging = ref(false);    // 文件悬在窗口上方（全屏提示）
      const dragOver = ref(false);    // 文件悬在输入区上方（只高亮输入框）
      const importNote = ref('');     // 上一次导入的统计，留在输入框下面

      // 外观完全跟随系统，这里只读它、不改它
      const themeName = ref(
        document.documentElement.getAttribute('data-theme') === 'dark' ? '夜间' : '日间'
      );
      function readTheme(t) {
        themeName.value = (t || document.documentElement.getAttribute('data-theme')) === 'dark'
          ? '夜间' : '日间';
      }
      window.addEventListener('themechange', function (e) {
        readTheme(e && e.detail ? e.detail.theme : '');
      });

      const raw = ref('');
      const strict = ref(true);
      const pdf = ref(true);
      const renderFB = ref(true);
      const ocrFB = ref(true);
      const doSum = ref(true);
      const trans = ref('off');

      const job = ref(null);
      const sel = ref(0);
      const history = ref([]);

      const saving = ref(false);
      const testing = ref(false);
      const testMsg = ref('');
      const testCls = ref('');
      const keyInput = ref('');

      const toast = reactive({ show: false, msg: '', cls: '' });
      let toastTimer = null;
      let pollTimer = null;

      const cfg = reactive({
        llm: { enabled: false, base_url: '', model: '', has_key: false, api_key_masked: '' },
        opt: { workers: 3, timeout: 40, min_chars: 400 },
      });

      const urls = computed(() => extractUrls(raw.value));
      const running = computed(() => !!job.value && job.value.state === 'running');
      const llmReady = computed(() => !!(cfg.llm.has_key && cfg.llm.enabled));

      const pct = computed(() => {
        if (!job.value) return 0;
        const total = job.value.total || 0;
        if (!total) return 0;
        if (job.value.state === 'running') {
          return Math.min(97, Math.round(((job.value.done || 0) / total) * 100));
        }
        return 100;
      });

      const stateText = computed(() => {
        if (!job.value) return '';
        const s = job.value.state;
        if (s === 'running') return '运行中';
        if (s === 'done') return '已完成';
        if (s === 'empty') return '全部未收录';
        if (s === 'error') return '出错';
        return job.value.msg || '结束';
      });

      const stateCls = computed(() => {
        if (!job.value) return '';
        const s = job.value.state;
        if (s === 'running') return 'run';
        if (s === 'done') return 'done';
        return 'fail';
      });

      const counts = computed(() => {
        const items = (job.value && job.value.items) || [];
        let ok = 0, skip = 0, chars = 0;
        for (let i = 0; i < items.length; i++) {
          const p = pillOf(items[i]);
          if (p.cls === 'ok') ok++;
          else if (p.cls === 'skip' || p.cls === 'bad') skip++;
          chars += items[i].chars || 0;
        }
        return {
          all: items.length, ok: ok, skip: skip, busy: items.length - ok - skip,
          chars: chars.toLocaleString(),
        };
      });

      /* 顶栏标题：有任务时用第一条有标题的条目，别让顶栏空着 */
      const crumb = computed(() => {
        if (view.value === 'history') return '历史任务';
        if (!job.value) return '新建任务';
        const items = job.value.items || [];
        for (let i = 0; i < items.length; i++) {
          if (items[i].title) return trim(items[i].title, 56);
        }
        return '任务 ' + job.value.id;
      });

      /* 中间：先按筛选、再按搜索词过滤 */
      const shownItems = computed(() => {
        const items = (job.value && job.value.items) || [];
        const kw = q.value.trim().toLowerCase();
        return items.filter((it) => {
          const ok = it.status === '完成' || it.file;
          if (filter.value === 'ok' && !ok) return false;
          if (filter.value === 'skip' && ok) return false;
          if (!kw) return true;
          return (it.title || '').toLowerCase().indexOf(kw) >= 0 ||
            (it.url || '').toLowerCase().indexOf(kw) >= 0;
        });
      });

      const filters = computed(() => [
        { k: 'all', n: '全部', c: counts.value.all },
        { k: 'ok', n: '已收录', c: counts.value.ok },
        { k: 'skip', n: '已剔除', c: counts.value.skip },
      ]);

      /* 右栏：本次配置 */
      const cfgChecks = computed(() => [
        { t: '严格模式：订阅墙与栏目页直接跳过', on: !!strict.value },
        { t: 'PDF 兜底：正文在附件里时去取 PDF', on: !!pdf.value },
        { t: renderFB.value ? '渲染兜底：反爬/超时时用本机浏览器重取' : '渲染兜底：未开启',
          on: !!renderFB.value },
        { t: ocrFB.value ? 'OCR 兜底：截图识别（有损，需校对）' : 'OCR 兜底：未开启',
          on: !!ocrFB.value },
        { t: doSum.value ? '摘要：' + (llmReady.value ? '大模型生成' : '本地提取式') : '摘要：未开启',
          on: !!doSum.value },
        { t: trans.value === 'off' ? '翻译：不翻译'
          : (trans.value === 'zh' ? '翻译：全文译成中文' : '翻译：中英对照'),
          on: trans.value !== 'off' },
      ]);

      /* 右栏：逐条进度 */
      const progressItems = computed(() => {
        const items = (job.value && job.value.items) || [];
        return items.map((it) => {
          const p = pillOf(it);
          let mark = '✓', cls = 'done';
          if (p.cls === 'busy' || p.cls === 'queued') { mark = '·'; cls = 'busy'; }
          else if (p.cls === 'skip' || p.cls === 'bad') { mark = '✕'; cls = 'fail'; }
          return {
            idx: it.idx, mark: mark, cls: cls,
            title: trim(it.title || shortUrl(it.url), 42),
          };
        });
      });

      /* 左栏：历史分组（今天 / 昨天 / 更早） */
      const histGroups = computed(() => {
        const today = dayOf(new Date());
        const groups = [];
        const map = {};
        history.value.forEach((h) => {
          const d = String(h.time || '').slice(0, 10);
          let name = '更早';
          if (d === today) name = '今天';
          else if (d === dayOf(new Date(Date.now() - 86400000))) name = '昨天';
          else if (d) name = d.slice(5).replace('-', '/');
          if (!map[name]) { map[name] = { name: name, items: [] }; groups.push(map[name]); }
          map[name].items.push(h);
        });
        return groups;
      });

      const keyPlaceholder = computed(() =>
        cfg.llm.has_key
          ? '已保存 ' + (cfg.llm.api_key_masked || '') + '，留空不改'
          : 'sk-...'
      );

      function dayOf(d) {
        const p = (n) => String(n).padStart(2, '0');
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
      }

      function relTime(t) {
        const s = String(t || '');
        if (!s) return '';
        const d = s.slice(0, 10);
        if (d === dayOf(new Date())) return s.slice(11, 16) || '今天';
        if (d === dayOf(new Date(Date.now() - 86400000))) return '昨天';
        return d.slice(5);
      }

      function tip(msg, cls) {
        toast.msg = msg;
        toast.cls = cls || '';
        toast.show = true;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { toast.show = false; }, 4200);
      }

      /* ------------------------------------------------ 表格拖放 / 导入 */

      // 去重用的键：把 utm 之类的尾巴和末尾斜杠抹平，否则同一篇会被当成两条
      function normKey(u) {
        return String(u || '')
          .replace(/[?&](utm_[^=&]+|ref|spm|from|share_token)=[^&]*/gi, '')
          .replace(/[?&]+$/, '')
          .replace(/\/+$/, '')
          .trim();
      }

      function hasFiles(e) {
        const dt = e && e.dataTransfer;
        if (!dt || !dt.types) return false;
        for (let i = 0; i < dt.types.length; i++) {
          if (dt.types[i] === 'Files') return true;
        }
        return false;
      }

      async function uploadSheet(file) {
        const r = await fetch('/api/parse-file?name=' + encodeURIComponent(file.name), {
          method: 'POST',
          headers: { 'Content-Type': 'application/octet-stream' },
          body: file,
        });
        let d = {};
        try { d = await r.json(); } catch (e) { /* 非 JSON */ }
        if (!r.ok) throw new Error(d.error || ('读取失败 HTTP ' + r.status));
        return d;
      }

      // 把新链接接到框里，已有的不重复加
      function mergeUrls(list) {
        const have = {};
        extractUrls(raw.value).forEach((u) => { have[normKey(u)] = 1; });
        const fresh = [];
        (list || []).forEach((u) => {
          const k = normKey(u);
          if (!k || have[k]) return;
          have[k] = 1;
          fresh.push(u);
        });
        if (fresh.length) {
          const head = raw.value.replace(/\s+$/, '');
          raw.value = (head ? head + '\n' : '') + fresh.join('\n');
        }
        return { added: fresh.length, existed: (list || []).length - fresh.length };
      }

      function noteOf(res, file, merged) {
        const seen = [];
        if (res.rows) seen.push(res.rows.toLocaleString() + ' 行');
        seen.push(res.cells.toLocaleString() + ' 个有内容的单元格');
        const bits = [];
        bits.push('识别 ' + res.count + ' 条链接');
        if (res.host_count) bits.push(res.host_count + ' 个站点');
        if (res.dups) bits.push('表内重复 ' + res.dups);
        if (res.upgraded) bits.push('补全 ' + res.upgraded + ' 条 www');
        if (merged.existed) bits.push(merged.existed + ' 条已在框里');
        let s = '「' + file.name + '」' + seen.join(' / ') + '：' + bits.join('，');
        if (res.truncated) s += '（超过上限，只取了前 ' + res.count + ' 条）';
        if (!merged.added) s += ' · 没有新链接可加';
        return s;
      }

      async function importFiles(files) {
        if (importing.value) return;
        const list = Array.prototype.slice.call(files || []);
        if (!list.length) return;

        if (view.value !== 'work') view.value = 'work';
        if (job.value) newTask();          // 拖着表格进来，就是开新的一批

        importing.value = true;
        dragOver.value = false;
        dragging.value = false;
        let added = 0;
        const lines = [];
        try {
          for (let i = 0; i < list.length; i++) {
            const f = list[i];
            try {
              const res = await uploadSheet(f);
              const merged = mergeUrls(res.urls || []);
              added += merged.added;
              lines.push(noteOf(res, f, merged));
            } catch (e) {
              lines.push('「' + f.name + '」没读成：' + e.message);
            }
          }
          importNote.value = lines.join('　｜　');
          tip(added ? ('导入 ' + added + ' 条链接') : '没有新增链接，详情看输入框下面',
              added ? '' : 'err');
        } finally {
          importing.value = false;
        }
      }

      function pickFile() {
        if (!importing.value && fileInput.value) fileInput.value.click();
      }

      function onPick(e) {
        const f = e.target.files && e.target.files[0];
        e.target.value = '';               // 同一个文件连拖两次也能触发
        if (f) importFiles([f]);
      }

      // 输入区自己的高亮（松手交给 window 那个统一处理，免得导两遍）
      function onDropOver() { dragOver.value = true; }
      function onDrop() { dragOver.value = false; dragging.value = false; }

      let lastOver = 0;
      let watchTimer = null;

      // 拖拽状态的兜底：dragover 会连续触发，1.2 秒没动静就认定拖拽已结束。
      // 只用 dragenter/dragleave 数层级容易被父子元素穿插搞成“卡住不消失”。
      function watchDrag() {
        if (watchTimer) return;
        watchTimer = setInterval(function () {
          if (!dragging.value || Date.now() - lastOver > 1200) {
            clearInterval(watchTimer);
            watchTimer = null;
            dragging.value = false;
            dragOver.value = false;
          }
        }, 400);
      }

      function onWinEnter(e) {
        if (!hasFiles(e)) return;
        lastOver = Date.now();
        dragging.value = true;
        watchDrag();
      }
      function onWinOver(e) {
        if (!hasFiles(e)) return;
        e.preventDefault();                // 不拦的话松手浏览器会直接打开文件
        lastOver = Date.now();
        if (!dragging.value) { dragging.value = true; }
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
      }
      function onWinLeave(e) {
        if (!hasFiles(e)) return;
        if (!e.relatedTarget) { dragging.value = false; dragOver.value = false; }
      }
      function onWinDrop(e) {
        if (!hasFiles(e)) return;
        e.preventDefault();
        dragging.value = false;
        dragOver.value = false;
        const fs = e.dataTransfer.files;
        if (fs && fs.length) importFiles(fs);
      }

      function pad(n) { return String(n).padStart(2, '0'); }

      function isOk(it) { return pillOf(it).cls === 'ok'; }

      function trim60(s) { return trim(s, 60); }

      /* 站点名跟域名是同一件事时（rhg.com vs rhg.com），元信息里只留一个 */
      function sameHost(it) {
        const site = String(it.site || '').toLowerCase().replace(/^www\./, '');
        if (!site) return true;
        let host = '';
        try { host = new URL(it.url).hostname.toLowerCase().replace(/^www\./, ''); }
        catch (e) { return false; }
        return !!host && (site === host || site.indexOf(host) >= 0 || host.indexOf(site) >= 0);
      }

      /* ---------- 视图切换 ---------- */
      function go(v) {
        view.value = v;
        if (v === 'history') loadHistory();
        nextTick(() => {
          const el = document.querySelector('.main-scroll');
          if (el) el.scrollTop = 0;
        });
      }

      function openCfg() {
        testMsg.value = '';
        cfgOpen.value = true;
      }

      /* ---------- 使用指南 ---------- */
      const guideOpen = ref(false);
      const guideHtml = ref('');
      const guideToc = ref([]);
      const guideActive = ref('');
      const guideBody = ref(null);
      let guideLoaded = false;

      function openGuide() {
        guideOpen.value = true;
        if (guideLoaded) return;
        fetch('/static/guide.md', { cache: 'no-store' })
          .then((r) => (r.ok ? r.text() : Promise.reject(r.status)))
          .then((md) => renderGuide(md))
          .catch(() => {
            guideHtml.value = '<p class="gd-err">读不到 static/guide.md。' +
              '如果你是自己改的包，确认这个文件还在。</p>';
          });
      }

      function renderGuide(md) {
        const box = document.createElement('div');
        if (window.marked && typeof window.marked.parse === 'function') {
          box.innerHTML = window.marked.parse(md, { gfm: true, breaks: false });
        } else {
          // 解析器没加载上（比如从旧包里升级）时别白屏，至少把原文摆出来
          const pre = document.createElement('pre');
          pre.textContent = md;
          box.appendChild(pre);
        }

        // 给 h2/h3 生成锚点，同时收集目录
        const toc = [];
        const used = Object.create(null);
        box.querySelectorAll('h2, h3').forEach((h, i) => {
          const text = (h.textContent || '').replace(/¶/g, '').trim();
          let base = 'g' + i + '-' + text
            .toLowerCase()
            .replace(/[^\w\u4e00-\u9fa5]+/g, '-')
            .replace(/^-+|-+$/g, '');
          let id = base || ('g' + i);
          if (used[id]) id = base + '-' + i;
          used[id] = 1;
          h.id = id;
          toc.push({ id: id, text: text, lv: h.tagName === 'H3' ? 3 : 2 });
        });

        // 外链另开一页，别把应用本身顶掉
        box.querySelectorAll('a[href^="http"]').forEach((a) => {
          a.target = '_blank';
          a.rel = 'noopener';
        });

        guideHtml.value = box.innerHTML;
        guideToc.value = toc;
        guideActive.value = toc.length ? toc[0].id : '';
        guideLoaded = true;
      }

      function onGuideScroll() {
        const el = guideBody.value;
        if (!el) return;
        const cr = el.getBoundingClientRect();
        let cur = '';
        el.querySelectorAll('h2, h3').forEach((h) => {
          if (h.getBoundingClientRect().top - cr.top <= 48) cur = h.id;
        });
        if (cur && cur !== guideActive.value) guideActive.value = cur;
      }

      function jumpGuide(id) {
        const el = guideBody.value;
        if (!el) return;
        const h = el.querySelector('#' + id);
        if (!h) return;
        const top = h.getBoundingClientRect().top - el.getBoundingClientRect().top
          + el.scrollTop - 14;
        el.scrollTo({ top: top, behavior: 'smooth' });
        guideActive.value = id;
      }

      function select(it) { sel.value = it.idx; }

      /* ---------- 配置 ---------- */
      async function loadCfg() {
        try {
          const c = await api('/api/config');
          Object.assign(cfg.llm, c.llm || {});
          Object.assign(cfg.opt, c.opt || {});
          keyInput.value = '';
          if (typeof c.opt.strict === 'boolean') strict.value = c.opt.strict;
          if (typeof c.opt.pdf_fallback === 'boolean') pdf.value = c.opt.pdf_fallback;
          if (typeof c.opt.render_fallback === 'boolean') renderFB.value = c.opt.render_fallback;
          if (typeof c.opt.ocr_fallback === 'boolean') ocrFB.value = c.opt.ocr_fallback;
          if (typeof c.opt.summary === 'boolean') doSum.value = c.opt.summary;
          if (c.opt.translate) trans.value = c.opt.translate;
        } catch (e) { /* 首次运行没有 config.json，用默认值 */ }
      }

      async function saveCfg() {
        saving.value = true;
        try {
          const body = {
            llm: {
              enabled: !!cfg.llm.enabled,
              base_url: (cfg.llm.base_url || '').trim(),
              model: (cfg.llm.model || '').trim(),
            },
            opt: {
              workers: cfg.opt.workers,
              timeout: cfg.opt.timeout,
              min_chars: cfg.opt.min_chars,
              strict: strict.value,
              pdf_fallback: pdf.value,
              render_fallback: renderFB.value,
              ocr_fallback: ocrFB.value,
              summary: doSum.value,
              translate: trans.value,
            },
          };
          if (keyInput.value.trim()) body.llm.api_key = keyInput.value.trim();
          const r = await api('/api/config', {
            method: 'POST', headers: JSON_HEAD, body: JSON.stringify(body),
          });
          Object.assign(cfg.llm, r.config.llm);
          Object.assign(cfg.opt, r.config.opt);
          keyInput.value = '';
          tip('设置已保存');
          return true;
        } catch (e) {
          tip('保存失败：' + e.message, 'err');
          return false;
        } finally {
          saving.value = false;
        }
      }

      async function testLLM() {
        testing.value = true;
        testMsg.value = '正在请求接口…';
        testCls.value = '';
        try {
          const body = {
            base_url: (cfg.llm.base_url || '').trim(),
            model: (cfg.llm.model || '').trim(),
          };
          if (keyInput.value.trim()) body.api_key = keyInput.value.trim();
          const r = await api('/api/test_llm', {
            method: 'POST', headers: JSON_HEAD, body: JSON.stringify(body),
          });
          if (r.ok) {
            testCls.value = 'ok';
            testMsg.value = '接口正常（' + (r.model || '') + '）：' + (r.sample || '').slice(0, 40);
          } else {
            testCls.value = 'bad';
            testMsg.value = r.error || '接口不可用';
          }
        } catch (e) {
          testCls.value = 'bad';
          testMsg.value = e.message;
        } finally {
          testing.value = false;
        }
      }

      /* ---------- 历史 ---------- */
      async function loadHistory() {
        try {
          const r = await api('/api/history');
          history.value = r.jobs || [];
        } catch (e) {
          history.value = [];
        }
      }

      async function openHist(id, silent) {
        try {
          const s = await api('/api/job/' + id);
          job.value = s;
          view.value = 'work';
          filter.value = 'all';
          q.value = '';
          // 把这次的链接回填到输入框，省得用户以为链接丢了；也方便改一改再跑
          raw.value = (s.items || []).map((x) => x.url).filter(Boolean).join('\n');
          const first = (s.items || []).filter((x) => x.status === '完成')[0];
          sel.value = first ? first.idx : 0;
          if (!silent) tip('已载入 ' + id + ' 的结果');
        } catch (e) {
          tip('载入失败：' + e.message, 'err');
        }
      }

      /* ---------- 任务 ---------- */
      function newTask() {
        clearTimeout(pollTimer);
        raw.value = '';
        job.value = null;
        sel.value = 0;
        filter.value = 'all';
        q.value = '';
        view.value = 'work';
        nextTick(() => {
          const el = document.querySelector('textarea');
          if (el) el.focus();
        });
      }

      async function start() {
        if (!urls.value.length || running.value) return;
        if (trans.value !== 'off' && !cfg.llm.has_key) {
          openCfg();
          tip('翻译需要先配置大模型接口', 'err');
          return;
        }
        try {
          const r = await api('/api/job', {
            method: 'POST',
            headers: JSON_HEAD,
            body: JSON.stringify({
              urls: urls.value,
              opt: {
                strict: strict.value,
                pdf_fallback: pdf.value,
                render_fallback: renderFB.value,
                ocr_fallback: ocrFB.value,
                summary: doSum.value,
                translate: trans.value,
                workers: cfg.opt.workers,
                timeout: cfg.opt.timeout,
                min_chars: cfg.opt.min_chars,
              },
              llm: { enabled: !!cfg.llm.enabled },
            }),
          });
          job.value = {
            id: r.id, total: r.total, state: 'running', done: 0, elapsed: 0, zip: '',
            items: urls.value.map((u, i) => ({
              idx: i + 1, url: u, status: '排队中', title: '', chars: 0,
              summary: '', reason: '', file: '', keywords: [],
            })),
          };
          sel.value = 0;
          filter.value = 'all';
          q.value = '';
          poll();
        } catch (e) {
          tip(e.message, 'err');
        }
      }

      async function poll() {
        clearTimeout(pollTimer);
        if (!job.value) return;
        const id = job.value.id;
        try {
          const s = await api('/api/job/' + id);
          if (!job.value || job.value.id !== id) return;
          job.value = s;
          if (s.state === 'running') {
            pollTimer = setTimeout(poll, 900);
          } else if (s.state === 'empty') {
            tip('没有一条抓取成功，看看是不是链接不对或网络受限', 'err');
          } else {
            // 跑完自动选中第一条收录的，主区直接有内容看
            if (!sel.value) {
              const first = (s.items || []).filter((x) => x.status === '完成')[0];
              if (first) sel.value = first.idx;
            }
            loadHistory();
          }
        } catch (e) {
          pollTimer = setTimeout(poll, 1600);
        }
      }

      function downloadZip() {
        if (job.value && job.value.zip) {
          window.location.href = '/api/job/' + job.value.id + '/zip';
        }
      }

      function openFile(it, kind) {
        if (!job.value) return;
        let rel = it.file;
        if (kind === 'docx') rel = it.docx_file;
        if (kind === 'md' && rel) rel = rel.replace(/^txt\//, 'md/').replace(/\.txt$/, '.md');
        if (!rel) return;
        window.location.href = '/api/job/' + job.value.id +
          '/file?path=' + encodeURIComponent(rel);
      }

      onMounted(() => {
        loadCfg();
        loadHistory();
        // 文件拖到窗口任何位置都接住：不接的话浏览器会把当前页换成这个文件
        window.addEventListener('dragenter', onWinEnter);
        window.addEventListener('dragover', onWinOver);
        window.addEventListener('dragleave', onWinLeave);
        window.addEventListener('drop', onWinDrop);
        // 支持 #history / #settings / #job=<id> 直接定位
        const h = (location.hash || '').replace('#', '');
        if (h === 'history') view.value = 'history';
        else if (h === 'settings') openCfg();
        else if (h === 'guide') openGuide();
        else if (h.indexOf('job=') === 0) openHist(h.slice(4), true);
      });

      return {
        icon: ICON, ver: VER, view, filter, q, cfgOpen, raw, strict, pdf, doSum, trans,
        renderFB, ocrFB,
        guideOpen, guideHtml, guideToc, guideActive, guideBody,
        openGuide, onGuideScroll, jumpGuide,
        job, sel, history, urls, running, pct, stateText, stateCls, counts,
        shownItems, filters, crumb, cfgChecks, progressItems, histGroups,
        llmReady, cfg, saving, testing, testMsg, testCls, keyInput, keyPlaceholder, toast,
        themeName, fileInput, importing, dragging, dragOver, importNote,
        pickFile, onPick, onDropOver, onDrop,
        go, openCfg, select, newTask, start, downloadZip, openFile,
        saveCfg, testLLM, openHist, relTime, sameHost,
        pillOf, detailOf, shortUrl, pad, isOk, trim60,
      };
    },
  };

  /* ------------------------------------------------------------ 挂载 */
  const app = createApp(App);

  const lib = window.ArcoVue || {};
  const plugin = lib.install ? lib : (lib.default || lib);
  if (plugin && typeof plugin.install === 'function') {
    app.use(plugin);
  } else {
    Object.keys(lib).forEach(function (k) {
      if (/^A[A-Z]/.test(k) && lib[k]) app.component(k, lib[k]);
    });
  }

  app.mount('#app');
})();
