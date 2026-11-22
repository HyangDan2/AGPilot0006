(() => {
  const root = document.getElementById('agtools-queue-preview');
  const get = (id) => root.querySelector('#aq-' + id);
  const base = new URL('api/v1/settings/embeddings', location.href);
  const queueUrl = new URL(base.href + '/queue');
  const pageSize = 15;
  const labels = {running:'처리 중', waiting:'대기', scheduled:'시간대 대기', retry:'재시도', failed:'실패', done:'완료', cancelled:'취소됨'};
  const reasons = {quiet_hours:'다음 색인 작업 시간에 시작합니다.', model_revision:'설정된 임베딩 모델 버전을 확인할 수 없습니다.',
    embedding_unavailable:'임베딩 서버가 응답하지 않았습니다.', embedding_dimension:'임베딩 벡터의 차원이 달라졌습니다.',
    chunk_read_failed:'저장된 문서 청크를 읽을 수 없습니다.'};
  const time = new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul', month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit', hourCycle:'h23'});
  let offset = 0;
  let selected = null;
  let queue = null;
  let settings = null;
  let dirty = false;
  let busy = false;
  let generation = 0;

  function announce(message, error = false) {
    get('message').textContent = message;
    get('message').toggleAttribute('data-error', error);
  }

  async function request(url, method = 'GET', body) {
    const options = {method, cache:'no-store'};
    if (body !== undefined) {
      const session = await request('/auth/session');
      if (!session.authenticated || !session.csrf) throw new Error('관리자 로그인이 필요합니다.');
      options.headers = {'Content-Type':'application/json', 'X-CSRF-Token':session.csrf};
      options.body = JSON.stringify(body);
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

  function render() {
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
      ? counts.running ? '현재 배치 완료 후 일시정지' : '색인 일시정지됨' : !settings?.enabled ? '의미 검색 설정으로 색인 대기' : '색인 실행 중';
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
      const [newQueue, newSettings] = await Promise.all([request(url),request(base)]);
      if (version !== generation || busy) return;
      if (offset && !newQueue.jobs.length) { offset = 0; return refresh(); }
      queue = newQueue;
      settings = newSettings;
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
    act(async () => { settings = await request(base, 'PUT', body); dirty = false; }, '검색 설정을 저장했습니다.');
  });
  root.querySelector('.aq-nav').addEventListener('click', (event) => {
    const button = event.target.closest('[data-view]');
    if (!button) return;
    const view = button.dataset.view;
    root.querySelectorAll('[data-view]').forEach((item) => {
      if (item === button) item.setAttribute('aria-current','page');
      else item.removeAttribute('aria-current');
    });
    for (const name of ['queue','settings']) get(name).hidden = name !== view;
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
