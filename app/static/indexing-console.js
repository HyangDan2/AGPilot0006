(() => {
  const root = document.getElementById('agtools-queue-preview');
  const get = (id) => root.querySelector('#aq-' + id);
  const base = new URL('api/v1/settings/embeddings', location.href);
  const queueUrl = new URL(base.href + '/queue');
  const searchBase = new URL('api/v1/settings/search', location.href);
  const searchOverviewUrl = new URL(searchBase.href + '/overview');
  const ftsBackfillUrl = new URL(searchBase.href + '/fts-backfill');
  const webSettingsUrl = new URL(searchBase.href + '/web');
  const webTestUrl = new URL(searchBase.href + '/web/test');
  const webCacheUrl = new URL(searchBase.href + '/web/cache');
  const docxToolUrl = new URL('api/v1/settings/tools/docx-to-md', location.href);
  let lastDocxResult = null;
  const pageSize = 15;
  const labels = {running:'처리 중', waiting:'대기', scheduled:'시간대 대기', retry:'재시도', failed:'실패', done:'완료', cancelled:'취소됨'};
  const searchModeLabels = {hybrid:'하이브리드', fts5_only:'FTS5', fts5_fallback:'FTS5 폴백', scan_fallback:'스캔 폴백', primary:'1순위', fallback:'폴백', cache_hit:'캐시 HIT', error:'오류'};
  const reasons = {quiet_hours:'다음 색인 작업 시간에 시작합니다.', model_revision:'설정된 임베딩 모델 버전을 확인할 수 없습니다.',
    embedding_unavailable:'임베딩 서버가 응답하지 않았습니다.', embedding_dimension:'임베딩 벡터의 차원이 달라졌습니다.',
    chunk_read_failed:'저장된 문서 청크를 읽을 수 없습니다.'};
  const time = new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul', month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit', hourCycle:'h23'});
  let offset = 0;
  let selected = null;
  let queue = null;
  let settings = null;
  let overview = null;
  let dirty = false;
  let webDirty = false;
  let busy = false;
  let generation = 0;

  function announce(message, error = false) {
    get('message').textContent = message;
    get('message').toggleAttribute('data-error', error);
  }

  async function request(url, method = 'GET', body) {
    const options = {method, cache:'no-store'};
    if (method !== 'GET' && method !== 'HEAD') {
      const session = await request('/auth/session');
      if (!session.authenticated || !session.csrf) throw new Error('관리자 로그인이 필요합니다.');
      options.headers = {'X-CSRF-Token':session.csrf};
      if (body !== undefined) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
      }
    }
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(response.status === 409 ? '작업 상태가 바뀌었습니다. 목록을 다시 확인해 주세요.' : '요청 실패 (HTTP ' + response.status + ')');
    return response.json();
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function clock(value) {
    if (!value) return '—';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? '—' : time.format(date);
  }
  function windowLabel(hour) { return String(hour).padStart(2, '0') + ':00'; }

  function renderDetail(job) {
    const detail = get('detail');
    detail.hidden = !job;
    if (!job) { detail.replaceChildren(); return; }
    const header = element('div', null, 'aq-panel-head');
    header.append(element('h3', job.filename));
    const action = ['retry','failed','cancelled'].includes(job.state) ? 'retry' : ['waiting','scheduled'].includes(job.state) ? 'cancel' : null;
    if (action) {
      const button = element('button', action === 'retry' ? '재시도' : '대기 취소', 'aq-button');
      button.type = 'button';
      button.disabled = busy;
      button.addEventListener('click', () => {
        if (action === 'cancel' && !window.confirm('이 문서의 대기 중인 색인 작업을 취소할까요?')) return;
        act(() => request(queueUrl.href + '/' + encodeURIComponent(job.document_id), 'POST', {action}),
            action === 'retry' ? '작업을 다시 등록했습니다.' : '대기 작업을 취소했습니다.');
      });
      header.append(button);
    }
    const defaultReason = {running:'현재 청크 배치를 처리하고 있습니다.', waiting:'앞선 작업이 끝나면 처리합니다.', done:'모든 청크를 색인했습니다.', cancelled:'대기 작업을 취소했습니다.'};
    let reason = reasons[job.reason] || defaultReason[job.state] || labels[job.state];
    if (job.state === 'retry' && job.retry_after) reason += ' 다음 재시도 ' + clock(job.retry_after);
    if (job.state === 'scheduled') reason += ' ' + windowLabel(queue.quiet_from) + '–' + windowLabel(queue.quiet_until) + ' KST';
    const dl = element('dl', null);
    for (const [key,value] of [['작업 ID',job.document_id],['등록',clock(job.created_at)],['시작',clock(job.started_at)],['최근 처리',clock(job.last_activity)],
      ['청크',job.processed_chunks + ' / ' + (job.total_chunks ?? '—')],['재시도 횟수',job.attempts ?? 0]]) {
      const cell = element('div',null);
      cell.append(element('dt',key),element('dd',value));
      dl.append(cell);
    }
    detail.replaceChildren(header,element('p',reason),dl);
  }

  function tenantLabel(t) {
    return t === 'public_tenant' ? '공개' : t === 'default_tenant' ? '관리자' : (t || '—');
  }

  function renderSearchLogs(tbodyId, emptyId, rows, showProvider) {
    const tbody = get(tbodyId);
    const empty = get(emptyId);
    if (!tbody) return;
    empty.hidden = rows.length > 0;
    tbody.replaceChildren(...rows.map((item) => {
      const tr = element('tr', null);
      const badgeText = showProvider
        ? (item.cache_hit ? '캐시 HIT (' + item.provider + ')' : (item.fallback_used ? '폴백 · ' + item.provider : item.provider))
        : (searchModeLabels[item.mode] || item.mode);
      const badge = element('span', badgeText, 'aq-badge');
      badge.dataset.state = item.mode;
      const tdMode = element('td', null);
      tdMode.append(badge);
      tr.append(
        element('td', clock(item.created_at), 'aq-time'),
        element('td', tenantLabel(item.tenant_id)),
        element('td', item.query),
        tdMode,
        element('td', (item.result_count ?? 0) + '건 · ' + (item.latency_ms ?? 0) + 'ms', 'aq-count')
      );
      return tr;
    }));
  }

  function renderSearchOverview() {
    if (!overview) return;
    const doc = overview.document_hybrid || {};
    const web = overview.web_search || {};
    const ws = web.settings || {};

    get('fts-count').textContent = (doc.fts_indexed_documents ?? 0) + ' / ' + (doc.total_documents ?? 0);
    get('tri-count').textContent = (doc.trigram_indexed_documents ?? 0) + ' / ' + (doc.total_documents ?? 0);
    get('doc-queries').textContent = doc.total_queries ?? 0;
    get('doc-latency').textContent = (doc.avg_latency_ms ?? 0) + 'ms';
    get('doc-ratio').textContent = '하이브리드 ' + (doc.hybrid_queries ?? 0) + '건 · 폴백 ' + (doc.fallback_queries ?? 0) + '건';
    renderSearchLogs('doc-logs', 'doc-logs-empty', doc.recent_logs || [], false);

    get('web-state').textContent = ws.enabled ? (ws.public_enabled ? '관리자+공개' : '관리자 전용') : '비활성';
    get('web-route').textContent = (ws.primary_provider || '—') + ' → ' + (ws.fallback_provider || 'none');
    get('web-cache').textContent = (web.cache_hit_rate ?? 0) + '%';
    get('web-latency').textContent = (web.avg_latency_ms ?? 0) + 'ms';
    get('web-cache-count').textContent = web.active_cache_entries ?? 0;
    get('web-ratio').textContent = '총 ' + (web.total_queries ?? 0) + '건 · 폴백 ' + (web.fallback_queries ?? 0) + '건';
    renderSearchLogs('web-logs', 'web-logs-empty', web.recent_logs || [], true);

    if (!webDirty && get('web-form')) {
      get('web-enabled').checked = !!ws.enabled;
      get('web-public').checked = !!ws.public_enabled;
      get('web-primary').value = ws.primary_provider || 'duckduckgo';
      get('web-fallback').value = ws.fallback_provider || 'wikipedia';
      get('web-searxng').value = ws.searxng_url || '';
      get('web-ttl').value = ws.cache_ttl_seconds ?? 900;
      get('web-pub-rpm').value = ws.public_rpm ?? 3;
      get('web-adm-rpm').value = ws.admin_rpm ?? 20;
      get('web-timeout').value = ws.timeout_seconds ?? 7;
    }
    if (get('fts-backfill')) get('fts-backfill').disabled = busy;
    if (get('web-clear-cache')) get('web-clear-cache').disabled = busy;
    if (get('web-test-btn')) get('web-test-btn').disabled = busy;
  }

  function render() {
    renderSearchOverview();
    if (settings) {
      get('model').textContent = settings.model || '임베딩 모델 미설정';
      get('search-status').textContent = settings.enabled ? '의미 검색 사용 중' : '키워드 검색만 사용';
      if (!dirty) {
        get('semantic').checked = settings.enabled;
        get('threshold').value = settings.min_similarity;
      }
      for (const control of get('form').elements) control.disabled = busy || !settings.configured;
    }
    if (!queue) return;
    const focusedJob = document.activeElement?.dataset.job;
    const counts = queue.counts;
    get('running-count').textContent = counts.running || 0;
    get('wait-count').textContent = (counts.waiting || 0) + (counts.scheduled || 0);
    get('error-count').textContent = (counts.retry || 0) + (counts.failed || 0);
    get('done-count').textContent = counts.done || 0;
    get('worker').textContent = !queue.configured ? '색인 모델 미설정' : queue.paused
      ? counts.running ? '현재 배치 완료 후 일시정지' : '색인 일시정지됨' : counts.running ? '색인 실행 중' : '색인 대기 중';
    get('worker').toggleAttribute('data-paused', queue.paused);
    get('pause').textContent = queue.paused ? '색인 재개' : '색인 일시정지';
    get('pause').disabled = busy || !queue.configured;
    get('empty').hidden = queue.jobs.length > 0;
    if (!queue.jobs.some((job) => job.document_id === selected)) selected = queue.jobs[0]?.document_id || null;
    get('rows').replaceChildren(...queue.jobs.map((job) => {
      const row = element('tr',null);
      row.toggleAttribute('data-selected',job.document_id === selected);
      const button = element('button',job.filename,'aq-doc');
      button.dataset.job = job.document_id;
      button.type = 'button';
      button.setAttribute('aria-pressed',String(job.document_id === selected));
      button.append(element('small',job.tenant_id === 'public_tenant' ? '공개' : job.tenant_id === 'default_tenant' ? '관리자' : job.tenant_id));
      const badge = element('span',labels[job.state],'aq-badge');
      badge.dataset.state = job.state;
      const progress = element('span',job.processed_chunks + ' / ' + (job.total_chunks ?? '—'),'aq-count');
      const cells = [element('td',null),element('td',null),element('td',null),element('td',clock(job.created_at),'aq-time')];
      cells[0].append(button); cells[1].append(badge); cells[2].append(progress);
      if (job.total_chunks > 0) {
        const bar = element('progress',null);
        bar.max = job.total_chunks;
        bar.value = job.processed_chunks;
        bar.setAttribute('aria-label','색인 청크 진행률');
        cells[2].append(bar);
      }
      row.append(...cells);
      return row;
    }));
    renderDetail(queue.jobs.find((job) => job.document_id === selected));
    if (focusedJob) [...get('rows').querySelectorAll('[data-job]')].find((button) => button.dataset.job === focusedJob)?.focus();
    get('pagination').hidden = queue.total <= pageSize && offset === 0;
    get('page').textContent = (Math.floor(offset / pageSize) + 1) + ' / ' + (Math.ceil(queue.total / pageSize) || 1);
    get('prev').disabled = offset === 0;
    get('next').disabled = offset + pageSize >= queue.total;
  }

  async function refresh() {
    if (busy) return;
    const version = ++generation;
    const url = new URL(queueUrl);
    url.search = new URLSearchParams({state:get('filter').value,limit:pageSize,offset});
    try {
      const [newQueue, newSettings, newOverview] = await Promise.all([
        request(url),
        request(base),
        request(searchOverviewUrl).catch(() => null),
      ]);
      if (version !== generation || busy) return;
      if (offset && !newQueue.jobs.length) { offset = 0; return refresh(); }
      queue = newQueue;
      settings = newSettings;
      if (newOverview) overview = newOverview;
      render();
    } catch (error) {
      if (version === generation) announce(error.message, true);
    }
  }

  async function act(action, success) {
    if (busy) return;
    busy = true;
    ++generation;
    render();
    try {
      await action();
      announce(success);
    } catch (error) {
      announce(error.message, true);
    } finally {
      busy = false;
      render();
      await refresh();
    }
  }

  get('rows').addEventListener('click', (event) => {
    const button = event.target.closest('[data-job]');
    if (button) { selected = button.dataset.job; render(); }
  });
  get('filter').addEventListener('change', () => { offset = 0; refresh(); });
  get('prev').addEventListener('click', () => { offset -= pageSize; refresh(); });
  get('next').addEventListener('click', () => { offset += pageSize; refresh(); });
  get('pause').addEventListener('click', () => {
    if (!queue) return;
    const paused = !queue.paused;
    act(async () => {
      const updated = await request(queueUrl, 'PUT', {paused});
      queue.paused = updated.paused;
    }, paused ? '색인 일시정지를 요청했습니다.' : '색인 작업을 재개했습니다.');
  });
  get('form').addEventListener('input', () => { dirty = true; });
  get('form').addEventListener('submit', (event) => {
    event.preventDefault();
    const body = {enabled:get('semantic').checked,min_similarity:get('threshold').valueAsNumber};
    act(async () => { settings = await request(base, 'PUT', body); dirty = false; }, '의미 검색 설정을 저장했습니다.');
  });
  if (get('web-form')) {
    get('web-form').addEventListener('input', () => { webDirty = true; });
    get('web-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const body = {
        enabled: get('web-enabled').checked,
        public_enabled: get('web-public').checked,
        primary_provider: get('web-primary').value,
        fallback_provider: get('web-fallback').value,
        searxng_url: get('web-searxng').value.trim(),
        cache_ttl_seconds: get('web-ttl').valueAsNumber,
        public_rpm: get('web-pub-rpm').valueAsNumber,
        admin_rpm: get('web-adm-rpm').valueAsNumber,
        timeout_seconds: get('web-timeout').valueAsNumber,
      };
      act(async () => {
        await request(webSettingsUrl, 'PUT', body);
        webDirty = false;
      }, '웹 검색 엔진 설정을 저장했습니다.');
    });
  }
  if (get('fts-backfill')) {
    get('fts-backfill').addEventListener('click', () => {
      act(async () => {
        const res = await request(ftsBackfillUrl, 'POST', {});
        announce('FTS5 백필 완료: 스캔 ' + res.scanned + '건 · 신규 색인 ' + res.indexed + '건');
      }, 'FTS5 일괄 백필을 완료했습니다.');
    });
  }
  if (get('web-clear-cache')) {
    get('web-clear-cache').addEventListener('click', () => {
      act(async () => {
        await request(webCacheUrl, 'DELETE');
      }, '웹 검색 캐시를 비웠습니다.');
    });
  }
  if (get('web-test-form')) {
    get('web-test-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const q = get('web-test-query').value.trim();
      if (!q) return;
      act(async () => {
        const res = await request(webTestUrl, 'POST', {query: q, limit: 5, lang: 'auto', bypass_cache: false});
        const meta = get('aq-web-test-meta') || get('web-test-meta');
        const list = get('web-test-results');
        if (meta) {
          meta.hidden = false;
          meta.textContent = '경로: ' + res.provider + (res.cache_hit ? ' (캐시 HIT)' : res.fallback_used ? ' (폴백)' : '') + ' · ' + res.total_results + '건 · ' + res.latency_ms + 'ms';
        }
        if (list) {
          list.hidden = false;
          list.replaceChildren(...(res.results || []).map((item) => {
            const li = element('li', null);
            const a = element('a', item.title || item.url);
            a.href = item.url;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            li.append(a);
            if (item.snippet) li.append(element('p', item.snippet));
            return li;
          }));
        }
      }, '웹 검색 테스트를 완료했습니다.');
    });
  }
  async function runDocxTool(responseFormat) {
    const fileInput = get('docx-file');
    const file = fileInput?.files?.[0];
    if (!file) { announce('DOCX 파일을 먼저 선택해 주세요.', true); return; }
    const session = await request('/auth/session');
    if (!session.authenticated || !session.csrf) throw new Error('관리자 로그인이 필요합니다.');
    const tableMode = get('docx-table-mode').value;
    const imageMode = get('docx-image-mode').value;
    const url = new URL(docxToolUrl);
    url.search = new URLSearchParams({ table_mode: tableMode, image_mode: imageMode, response_format: responseFormat });
    const form = new FormData();
    form.append('file', file, file.name);
    const resp = await fetch(url, { method: 'POST', headers: { 'X-CSRF-Token': session.csrf }, body: form });
    if (!resp.ok) {
      let msg = '변환 실패 (HTTP ' + resp.status + ')';
      try { const j = await resp.json(); if (j?.detail) msg = j.detail; } catch {}
      throw new Error(msg);
    }
    if (responseFormat === 'zip') {
      const blob = await resp.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = file.name.replace(/\.docx$/i, '') + '.zip';
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 3000);
      return;
    }
    const data = await resp.json();
    lastDocxResult = data;
    get('docx-output').hidden = false;
    get('docx-stat-sec').textContent = data.stats?.sections ?? 0;
    get('docx-stat-tbl').textContent = data.stats?.tables ?? 0;
    get('docx-stat-mtbl').textContent = data.stats?.merged_tables ?? 0;
    get('docx-stat-img').textContent = data.stats?.images ?? 0;
    get('docx-md').value = data.markdown || '';

    const secs = data.sections || [];
    get('docx-sec-panel').hidden = secs.length === 0;
    get('docx-sec-rows').replaceChildren(...secs.map((s) => {
      const tr = element('tr', null);
      tr.append(
        element('td', s.section_id),
        element('td', '  '.repeat(Math.max(0, (s.level || 1) - 1)) + s.title),
        element('td', String(s.table_count ?? 0), 'aq-count'),
        element('td', (s.figure_ids || []).join(', ') || '—')
      );
      return tr;
    }));

    const figs = data.figures || [];
    get('docx-fig-panel').hidden = figs.length === 0;
    get('docx-figs').replaceChildren(...figs.map((f) => {
      const card = element('div', null, 'aq-fig-card');
      if (f.preview_data_url) {
        const img = document.createElement('img');
        img.src = f.preview_data_url;
        img.alt = f.caption || f.figure_id;
        card.append(img);
      }
      card.append(
        element('strong', f.figure_id),
        element('small', (f.section_id || '—') + ' · ' + f.context),
        element('small', f.caption || f.filename)
      );
      return card;
    }));
  }

  if (get('docx-form')) {
    get('docx-form').addEventListener('submit', (event) => {
      event.preventDefault();
      act(() => runDocxTool('json'), 'DOCX → Markdown 추출을 완료했습니다.');
    });
    get('docx-zip').addEventListener('click', () => {
      act(() => runDocxTool('zip'), 'Markdown + 사진 ZIP 패키지를 다운로드했습니다.');
    });
    get('docx-copy').addEventListener('click', async () => {
      if (!lastDocxResult?.markdown) return;
      try {
        await navigator.clipboard.writeText(lastDocxResult.markdown);
        announce('Markdown 원문을 클립보드에 복사했습니다.');
      } catch {
        announce('클립보드 복사에 실패했습니다.', true);
      }
    });
    get('docx-dl-md').addEventListener('click', () => {
      if (!lastDocxResult?.markdown) return;
      const blob = new Blob([lastDocxResult.markdown], { type: 'text/markdown;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (lastDocxResult.filename || 'document').replace(/\.docx$/i, '') + '.md';
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    });
  }

  root.querySelector('.aq-nav').addEventListener('click', (event) => {
    const button = event.target.closest('[data-view]');
    if (!button) return;
    const view = button.dataset.view;
    root.querySelectorAll('[data-view]').forEach((item) => {
      if (item === button) item.setAttribute('aria-current','page');
      else item.removeAttribute('aria-current');
    });
    for (const name of ['queue','search','docx','settings']) {
      if (get(name)) get(name).hidden = name !== view;
    }
  });
  const themeButton = document.getElementById('themeFab');
  function applyTheme(value) {
    const dark = value === 'dark';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.querySelector('meta[name="theme-color"]').content = dark ? '#0B1020' : '#FAFAFD';
    const label = dark ? '라이트 모드로 전환' : '다크 모드로 전환';
    themeButton.setAttribute('aria-label',label);
    themeButton.title = label;
    themeButton.setAttribute('aria-pressed',String(dark));
  }
  try { applyTheme(localStorage.getItem('hd2-theme')); } catch { applyTheme('light'); }
  themeButton.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem('hd2-theme',next); } catch { /* Theme still applies to this tab. */ }
  });
  window.addEventListener('storage', (event) => {
    if (event.key === 'hd2-theme') applyTheme(event.newValue);
  });
  refresh();
  setInterval(() => { if (!document.hidden && !get('detail').contains(document.activeElement)) refresh(); }, 5000);
})();
