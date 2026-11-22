const form = document.querySelector('#embedding-settings');
const endpoint = new URL('api/v1/settings/embeddings', location.href);
const summary = document.querySelector('#embedding-summary');
const feedback = document.querySelector('#embedding-feedback');
const enabled = form.elements.enabled;
const similarity = form.elements.min_similarity;
const save = form.querySelector('button[type="submit"]');
let dirty = false;
let saving = false;

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function render(settings) {
  const {model, configured, indexed_documents, pending_documents, retrying_documents} = settings;
  summary.textContent = configured
    ? `${model} · 색인 완료 ${indexed_documents} · 대기 ${pending_documents} · 재시도 ${retrying_documents}`
    : '서버에 임베딩 모델을 설정해야 합니다.';
  if (!dirty) {
    enabled.checked = settings.enabled;
    similarity.value = settings.min_similarity;
  }
  enabled.disabled = similarity.disabled = save.disabled = !configured || saving;
}

async function refresh() {
  if (saving) return;
  try {
    const settings = await request(endpoint);
    if (!saving) render(settings);
  } catch (error) {
    summary.textContent = `설정을 불러오지 못했습니다. (${error.message})`;
  }
}

form.addEventListener('input', () => { dirty = true; });
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  saving = save.disabled = true;
  try {
    const session = await request('/auth/session');
    if (!session.authenticated) throw new Error('관리자 로그인 필요');
    const settings = await request(endpoint, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf},
      body: JSON.stringify({enabled: enabled.checked, min_similarity: similarity.valueAsNumber}),
    });
    dirty = saving = false;
    render(settings);
    feedback.textContent = '저장했습니다.';
  } catch (error) {
    saving = save.disabled = false;
    feedback.textContent = `설정을 저장하지 못했습니다. (${error.message})`;
  }
});

refresh();
setInterval(refresh, 5000);
