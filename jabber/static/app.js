'use strict';

let APP_NAME = 'Jabber';
(function () {
  const rawFetch = window.fetch.bind(window);
  let loginShown = false;
  async function showLogin() {
    if (loginShown) return;
    loginShown = true;
    let needsSetup = false;
    try { needsSetup = (await (await rawFetch('/api/me')).json()).needs_setup; } catch (e) {}
    document.getElementById('loginTitle').textContent = needsSetup ? 'Create the first admin account' : 'Sign in';
    document.getElementById('loginBtn').textContent = needsSetup ? 'Create account' : 'Sign in';
    document.getElementById('loginPass').autocomplete = needsSetup ? 'new-password' : 'current-password';
    document.getElementById('loginOverlay').hidden = false;
    document.getElementById('loginForm').dataset.setup = needsSetup ? '1' : '';
    document.getElementById('loginUser').focus();
  }
  window.fetch = async function (url, opt) {
    opt = opt ? Object.assign({}, opt) : {};
    const method = (opt.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      opt.headers = Object.assign({ 'Content-Type': 'application/json' }, opt.headers || {});
      if (opt.body === undefined && method === 'POST') opt.body = '{}';
    }
    const resp = await rawFetch(url, opt);
    if (resp.status === 401 && !/\/api\/(login|setup|me)\b/.test(String(url))) showLogin();
    return resp;
  };
  document.addEventListener('DOMContentLoaded', async () => {
    const form = document.getElementById('loginForm');
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const err = document.getElementById('loginErr');
      const btn = document.getElementById('loginBtn');
      err.textContent = '';
      btn.disabled = true;
      try {
        const r = await rawFetch(form.dataset.setup ? '/api/setup' : '/api/login', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: document.getElementById('loginUser').value, password: document.getElementById('loginPass').value }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
        location.reload();
      } catch (e) { err.textContent = e.message; }
      btn.disabled = false;
    });
    try {
      const me = await (await rawFetch('/api/me')).json();
      APP_NAME = me.instance_name || APP_NAME;
      document.title = APP_NAME;
      document.getElementById('brandName').textContent = APP_NAME;
      window.JABBER_ME = me;
      window.JABBER_FEATURES = me.features || {};
      initWebSetting();
      if (!(me.features && me.features.files)) document.body.classList.add('no-files');
      if (!me.authenticated) { showLogin(); return; }
      if (me.provider !== 'none') {
        const out = document.createElement('button');
        out.className = 'iconbtn';
        out.title = 'Sign out';
        out.textContent = '\u23FB';
        out.addEventListener('click', async () => { await fetch('/api/logout', { method: 'POST' }); location.reload(); });
        document.querySelector('.badges').appendChild(out);
      }
    } catch (e) {}
  });
})();
const ACTIONS = [
  function () { togglePanel('sidebar') },
  function () { toggleThemeQuick() },
  function () { document.getElementById('settingsOverlay').classList.add('open') },
  function () { toggleSidebarSection('projects') },
  function () { toggleSidebarSection('history') },
  function () { clearProjectFiles() },
  function () { document.getElementById('projSettingsUploadFolderInput').click() },
  function () { document.getElementById('projSettingsUploadFilesInput').click() },
  function () { saveProjectSettings() },
  function () { deleteProjectFromSettings() },
  function () { document.getElementById('projectSettingsOverlay').classList.remove('open') },
  function () { document.getElementById('filePreviewOverlay').classList.remove('open') },
  function () { startChipPrompt('Can you search the web for something?') },
  function () { startChipPrompt('What can you help me with here?') },
  function () { startChipPrompt('Can I upload files or paste screenshots into chat?') },
  function () { exportAllConversations() },
  function () { clearAllConversations() },
  function () { document.getElementById('settingsOverlay').classList.remove('open') },
  function () { copyCodeBlock(this) },
  function () { this.parentElement.classList.toggle('open') }
];
document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-act]');
  if (el) ACTIONS[+el.dataset.act].call(el, ev);
});
document.addEventListener('change', (ev) => {
  const el = ev.target.closest('[data-chg]');
  if (el) ACTIONS[+el.dataset.chg].call(el, ev);
});

const messagesEl = document.getElementById('messages');
const inputBox = document.getElementById('inputBox');
const sendBtn = document.getElementById('sendBtn');
const convListEl = document.getElementById('convList');
const projListEl = document.getElementById('projList');
const newChatBtn = document.getElementById('newChatBtn');
const newProjectBtn = document.getElementById('newProjectBtn');
const searchBox = document.getElementById('searchBox');

let currentConvId = null;
let allConvs = []; // cached list, re-filtered client-side by searchBox

let allProjects = [];
let currentProjectId = null;

let currentUser = { display_name: null, email: null };

async function loadCurrentUser() {
  try {
    const resp = await fetch('/api/me');
    const data = await resp.json();
    currentUser = data;
    const greeting = document.getElementById('heroGreeting');
    if (greeting && data.display_name) {
      greeting.textContent = `What's the move, ${data.display_name}?`;
    }
  } catch (e) {
  }
}

function myDisplayName() {
  return currentUser.display_name || 'You';
}

async function loadProjects() {
  try {
    const resp = await fetch('/api/projects');
    const data = await resp.json();
    allProjects = data.projects || [];
    renderProjList();
  } catch (e) { /* server unreachable -- leave stale list rather than blank it */ }
}

const PROJECT_COLORS = ['#5a82c8', '#b46fd1', '#3aa99f', '#d99a2b', '#c0392b', '#62a945', '#7d7ddb'];
function projectColor(idx) { return PROJECT_COLORS[idx % PROJECT_COLORS.length]; }

function renderProjList() {
  projListEl.innerHTML = '';
  const allItem = document.createElement('div');
  allItem.className = 'proj proj-all' + (currentProjectId === null ? ' active' : '');
  allItem.innerHTML = `<span class="proj-icon">*</span><span class="projtitle">All conversations</span>`;
  allItem.addEventListener('click', () => { currentProjectId = null; renderProjList(); renderConvList(); });
  projListEl.appendChild(allItem);
  allProjects.forEach((p, idx) => {
    const row = document.createElement('div');
    row.className = 'proj' + (p.id === currentProjectId ? ' active' : '');
    row.title = p.name;
    const initial = (p.name.trim()[0] || '?').toUpperCase();
    row.innerHTML = `<span class="proj-icon" style="background:${projectColor(idx)}">${initial}</span><span class="projtitle">${p.name}</span>` +
      (p.has_files ? `<span title="Has uploaded files" style="flex:none">📁</span>` : '') +
      `<button class="convdel projsettingsbtn" title="Project settings">✎</button>`;
    row.addEventListener('click', (e) => {
      if (e.target.closest('.projsettingsbtn')) return; // handled separately below
      currentProjectId = p.id; renderProjList(); renderConvList();
    });
    row.querySelector('.projsettingsbtn').addEventListener('click', (e) => {
      e.stopPropagation();
      openProjectSettings(p.id);
    });
    projListEl.appendChild(row);
  });
}

let projSettingsCwd = '.';

function renderProjSettingsFolderLabel(proj) {
  const pathEl = document.getElementById('projSettingsFolderPath');
  const clearBtn = document.getElementById('projSettingsClearBtn');
  if (!proj.has_files) {
    pathEl.textContent = 'Nothing uploaded yet';
    clearBtn.style.display = 'none';
  } else {
    pathEl.textContent = '📤 Files uploaded from your device';
    clearBtn.style.display = '';
  }
}

async function readAndUploadFiles(projId, fileList, onProgress) {
  const SKIP_DIRS = ['node_modules/', '.git/', '__pycache__/', '.venv/', 'dist/', 'build/'];
  const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];
  const extOf = (n) => { const i = n.lastIndexOf('.'); return i === -1 ? '' : n.slice(i).toLowerCase(); };
  const maxBytes = (window.JABBER_FEATURES && window.JABBER_FEATURES.max_upload_bytes) || 2 * 1024 ** 3;
  const written = [], problems = [];
  const list = Array.from(fileList).filter((f) => {
    const p = f.webkitRelativePath || f.name;
    return !SKIP_DIRS.some((d) => p.includes('/' + d) || p.startsWith(d)) && !p.split('/').some((s) => s.startsWith('.'));
  });
  if (!list.length) return { ok: false, note: 'Nothing to upload in that pick (hidden and build folders are skipped).' };
  const post = async (url, body) => {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    return { status: r.status, data: await r.json().catch(() => ({})) };
  };
  for (let i = 0; i < list.length; i++) {
    const f = list[i];
    let path = f.webkitRelativePath || f.name;
    let blob = f;
    if (IMAGE_EXTS.includes(extOf(f.name)) && f.size > 1_500_000) {
      try {
        const small = await downscaleImageIfNeeded(f);
        if (small && small !== f) {
          blob = small;
          if (small.type === 'image/jpeg' && !/\.jpe?g$/i.test(path)) path = path.replace(/\.[^./]+$/, '') + '.jpg';
        }
      } catch (e) { /* keep the original */ }
    }
    if (blob.size > maxBytes) { problems.push(`${path}: over the ${Math.round(maxBytes / 2 ** 20)} MB file limit`); continue; }
    if (onProgress) onProgress({ index: i + 1, total: list.length, name: f.name, sent: 0, size: blob.size });
    const begin = await post(`/api/projects/${projId}/upload/begin`, { path, size: blob.size });
    if (begin.status !== 200) {
      problems.push(begin.data.error || `${path}: could not start upload`);
      if (begin.status === 413 && /quota/.test(begin.data.error || '')) break;
      continue;
    }
    const uid = begin.data.upload_id, step = begin.data.chunk_bytes || 8 * 2 ** 20;
    let off = 0, failed = false;
    do {
      const piece = blob.slice(off, off + step);
      let resp;
      try {
        resp = await fetch(`/api/uploads/${uid}/chunk?offset=${off}`, {
          method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: piece });
      } catch (e) { problems.push(`${path}: connection lost`); failed = true; break; }
      if (!resp.ok) {
        const d = await resp.json().catch(() => ({}));
        problems.push(d.error || (resp.status === 413 ? `${path}: the web server in front of Jabber rejected a ${Math.round(piece.size / 1024)} KB piece (its body limit is too low)` : `${path}: upload failed (HTTP ${resp.status})`));
        failed = true;
        break;
      }
      off += piece.size;
      if (onProgress) onProgress({ index: i + 1, total: list.length, name: f.name, sent: off, size: blob.size });
    } while (off < blob.size);
    if (failed) { await post(`/api/uploads/${uid}/abort`); continue; }
    const fin = await post(`/api/uploads/${uid}/finish`);
    if (fin.status !== 200) { problems.push(fin.data.error || `${path}: could not be read`); continue; }
    written.push(...fin.data.written_paths);
    (fin.data.notes || []).forEach((n) => problems.push(n));
  }
  if (!written.length) return { ok: false, note: problems.length ? problems.slice(0, 3).join(' \u00b7 ') : 'Nothing was uploaded.' };
  return { ok: true, written: written.length, skipped: list.length - written.length, notes: problems, writtenPaths: written };
}

function uploadProgressText(el) {
  return (p) => {
    const pct = p.size ? Math.floor((p.sent / p.size) * 100) : 100;
    el.textContent = `Uploading ${p.index}/${p.total}: ${p.name}${p.size > 2 ** 20 ? ' (' + pct + '%)' : ''}`;
  };
}

function uploadSummary(r) {
  let t = `Uploaded ${r.written} file${r.written === 1 ? '' : 's'}.`;
  if (r.skipped) t += ` ${r.skipped} skipped.`;
  if (r.notes && r.notes.length) t += ' ' + r.notes.slice(0, 3).join(' \u00b7 ') + (r.notes.length > 3 ? ` (+${r.notes.length - 3} more)` : '');
  return t;
}

async function uploadFileList(fileList) {
  const projId = document.getElementById('projSettingsId').value;
  const status = document.getElementById('projSettingsUploadStatus');
  if (!fileList.length) return;
  status.textContent = `Reading ${fileList.length} file(s)…`;
  const result = await readAndUploadFiles(projId, fileList, uploadProgressText(status));
  if (!result.ok) { status.textContent = result.note; return; }
  status.textContent = uploadSummary(result);
  const projResp = await fetch(`/api/projects/${projId}`);
  const proj = await projResp.json();
  renderProjSettingsFolderLabel(proj);
  projSettingsCwd = '.';
  await loadProjectFiles(projId);
  await loadProjects();
  renderProjList();
}

async function clearProjectFiles() {
  const projId = document.getElementById('projSettingsId').value;
  const status = document.getElementById('projSettingsUploadStatus');
  if (!confirm('Clear this project\'s uploaded files? Cannot be undone.')) return;
  status.textContent = 'Clearing…';
  try {
    const resp = await fetch(`/api/projects/${projId}/clear-files`, { method: 'POST' });
    const proj = await resp.json();
    renderProjSettingsFolderLabel(proj);
    status.textContent = 'Cleared.';
    projSettingsCwd = '.';
    await loadProjectFiles(projId);
    await loadProjects();
    renderProjList();
  } catch (e) {
    status.textContent = 'Could not reach the server.';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const folderInput = document.getElementById('projSettingsUploadFolderInput');
  const filesInput = document.getElementById('projSettingsUploadFilesInput');
  if (folderInput) folderInput.addEventListener('change', () => {
    const list = Array.from(folderInput.files || []);
    folderInput.value = ''; // reset so picking the same folder again still fires 'change'
    uploadFileList(list);
  });
  if (filesInput) filesInput.addEventListener('change', () => {
    const list = Array.from(filesInput.files || []);
    filesInput.value = '';
    uploadFileList(list);
  });
});

async function openProjectSettings(projId) {
  document.getElementById('projSettingsId').value = projId;
  document.getElementById('projSettingsStatus').textContent = 'Loading…';
  document.getElementById('projectSettingsOverlay').classList.add('open');
  try {
    const resp = await fetch(`/api/projects/${projId}`);
    const proj = await resp.json();
    if (proj.error) { document.getElementById('projSettingsStatus').textContent = proj.error; return; }
    document.getElementById('projSettingsName').value = proj.name || '';
    document.getElementById('projSettingsInstructions').value = proj.instructions || '';
    renderProjSettingsFolderLabel(proj);
    document.getElementById('projSettingsStatus').textContent = '';
    document.getElementById('projSettingsUploadStatus').textContent = '';
    projSettingsCwd = '.';
    await loadProjectFiles(projId);
  } catch (e) {
    document.getElementById('projSettingsStatus').textContent = 'Could not reach the server.';
  }
}

async function loadProjectFiles(projId) {
  const box = document.getElementById('projSettingsFiles');
  box.innerHTML = '<div class="pf-empty">Loading…</div>';
  try {
    const resp = await fetch(`/api/projects/${projId}/files?path=${encodeURIComponent(projSettingsCwd)}`);
    const data = await resp.json();
    if (data.error) { box.innerHTML = ''; box.appendChild(pfEmpty(data.error)); return; }
    box.innerHTML = '';
    if (projSettingsCwd !== '.') {
      const crumb = document.createElement('div');
      crumb.className = 'pf-crumb';
      const root = document.createElement('span');
      root.textContent = 'root';
      root.addEventListener('click', () => { projSettingsCwd = '.'; loadProjectFiles(projId); });
      crumb.appendChild(root);
      crumb.appendChild(document.createTextNode(' / ' + projSettingsCwd));
      box.appendChild(crumb);
    }
    const entries = data.entries || [];
    if (!entries.length) {
      box.appendChild(pfEmpty('Empty folder'));
    }
    for (const name of entries) {
      const isDir = name.endsWith('/');
      const nextPath = projSettingsCwd === '.' ? name.replace(/\/$/, '') : `${projSettingsCwd}/${name.replace(/\/$/, '')}`;
      const row = document.createElement('div');
      row.className = 'pf-entry';
      row.textContent = (isDir ? '📁 ' : '📄 ') + name;
      row.addEventListener('click', () => {
        if (isDir) { projSettingsCwd = nextPath; loadProjectFiles(projId); }
        else { openFilePreview(projId, nextPath); }
      });
      box.appendChild(row);
    }
    if (data.truncated) box.appendChild(pfEmpty('…more than 200 entries, truncated'));
  } catch (e) {
    box.innerHTML = '';
    box.appendChild(pfEmpty('Could not reach the server'));
  }
}

function pfEmpty(text) {
  const div = document.createElement('div');
  div.className = 'pf-empty';
  div.textContent = text;
  return div;
}

async function saveProjectSettings() {
  const projId = document.getElementById('projSettingsId').value;
  const status = document.getElementById('projSettingsStatus');
  status.textContent = 'Saving…';
  try {
    const resp = await fetch(`/api/projects/${projId}/update`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: document.getElementById('projSettingsName').value,
        instructions: document.getElementById('projSettingsInstructions').value,
      }),
    });
    const data = await resp.json();
    if (data.error) { status.textContent = 'Error: ' + data.error; return; }
    status.textContent = 'Saved.';
    await loadProjects();
    renderProjList();
  } catch (e) {
    status.textContent = 'Could not reach the server.';
  }
}

async function deleteProjectFromSettings() {
  const projId = document.getElementById('projSettingsId').value;
  const name = document.getElementById('projSettingsName').value;
  if (!confirm(`Delete project "${name}"? Its conversations stay, just become unfiled. Cannot be undone.`)) return;
  try { await fetch(`/api/projects/${projId}`, { method: 'DELETE' }); } catch (e) {}
  document.getElementById('projectSettingsOverlay').classList.remove('open');
  if (currentProjectId === projId) currentProjectId = null;
  await loadProjects();
  renderProjList();
  renderConvList();
}

async function openFilePreview(projId, path) {
  const overlay = document.getElementById('filePreviewOverlay');
  const title = document.getElementById('filePreviewTitle');
  const body = document.getElementById('filePreviewBody');
  title.textContent = path;
  body.textContent = 'Loading…';
  overlay.classList.add('open');
  try {
    const resp = await fetch(`/api/projects/${projId}/file?path=${encodeURIComponent(path)}`);
    const data = await resp.json();
    body.textContent = data.error ? `Error: ${data.error}` : data.content;
  } catch (e) {
    body.textContent = 'Could not reach the server.';
  }
}

async function createProject() {
  const name = prompt('Project name:');
  if (!name || !name.trim()) return;
  try {
    const resp = await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim() }),
    });
    const data = await resp.json();
    if (data.error) { alert(data.error); return; }
    await loadProjects();
    currentProjectId = data.id;
    renderProjList();
    renderConvList();
  } catch (e) { alert('Could not create project -- server unreachable?'); }
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const HL_LANG_ALIASES = {
  ps1: 'powershell', pwsh: 'powershell', powershell: 'powershell',
  py: 'python', python: 'python', python3: 'python',
  sh: 'bash', bash: 'bash', shell: 'bash', zsh: 'bash', console: 'bash',
  js: 'javascript', javascript: 'javascript', ts: 'javascript', typescript: 'javascript', jsx: 'javascript', tsx: 'javascript',
  json: 'json', jsonc: 'json',
  yaml: 'yaml', yml: 'yaml',
  sql: 'sql',
};

const HL_SPECS = {
  powershell: {
    ci: true,
    comment: /(?:#.*$)|(?:<#[\s\S]*?#>)/m,
    string: /"(?:[^"`]|`.)*"|'(?:[^']|'')*'/,
    variable: /\$\w+/,
    number: /\b\d+(?:\.\d+)?\b/,
    keyword: /\b(function|param|if|elseif|else|foreach|for|while|do|until|switch|try|catch|finally|return|break|continue|begin|process|end|throw|class|enum|using|in|and|or|not|new-object|write-host|write-output|write-error|write-warning)\b/,
  },
  python: {
    comment: /#.*$/m,
    string: /("""[\s\S]*?""")|('''[\s\S]*?''')|("(?:[^"\\]|\\.)*")|('(?:[^'\\]|\\.)*')/,
    number: /\b\d+(?:\.\d+)?\b/,
    keyword: /\b(def|class|import|from|as|return|if|elif|else|for|while|in|not|and|or|is|try|except|finally|raise|with|yield|lambda|pass|break|continue|global|nonlocal|None|True|False|self|async|await)\b/,
  },
  bash: {
    comment: /#.*$/m,
    string: /"(?:[^"\\]|\\.)*"|'[^']*'/,
    variable: /\$\{?\w+\}?/,
    keyword: /\b(if|then|else|elif|fi|for|while|do|done|case|esac|function|return|break|continue|local|export|echo|in)\b/,
  },
  javascript: {
    comment: /(?:\/\/.*$)|(?:\/\*[\s\S]*?\*\/)/m,
    string: /`(?:[^`\\]|\\.)*`|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/,
    number: /\b\d+(?:\.\d+)?\b/,
    keyword: /\b(function|const|let|var|if|else|for|while|do|return|class|extends|new|this|typeof|instanceof|try|catch|finally|throw|async|await|import|export|default|from|of|in|null|undefined|true|false)\b/,
  },
  json: {
    string: /"(?:[^"\\]|\\.)*"/,
    number: /-?\b\d+(?:\.\d+)?\b/,
    keyword: /\b(true|false|null)\b/,
  },
  yaml: {
    comment: /#.*$/m,
    string: /"(?:[^"\\]|\\.)*"|'[^']*'/,
    keyword: /\b(true|false|null|yes|no)\b/i,
  },
  sql: {
    ci: true,
    comment: /(?:--.*$)|(?:\/\*[\s\S]*?\*\/)/m,
    string: /'(?:[^']|'')*'/,
    number: /\b\d+(?:\.\d+)?\b/,
    keyword: /\b(select|from|where|join|left|right|inner|outer|on|group|by|order|having|insert|into|values|update|set|delete|create|table|alter|drop|and|or|not|null|as|distinct|limit|union|all)\b/,
  },
};

function highlightCode(rawCode, lang) {
  const escaped = escapeHtml(rawCode);
  const key = HL_LANG_ALIASES[(lang || '').toLowerCase()];
  const spec = key && HL_SPECS[key];
  if (!spec) return escaped;
  const order = ['comment', 'string', 'variable', 'number', 'keyword'];
  const parts = order.filter(k => spec[k]).map(k => `(?<${k}>${spec[k].source})`);
  if (!parts.length) return escaped;
  const re = new RegExp(parts.join('|'), 'gm' + (spec.ci ? 'i' : ''));
  return escaped.replace(re, (match, ...args) => {
    const groups = args[args.length - 1];
    for (const k of order) {
      if (groups && groups[k] !== undefined) return `<span class="tok-${k}">${match}</span>`;
    }
    return match;
  });
}

function renderMarkdown(raw) {
  const blocks = [];
  let text = raw.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push({ lang, code });
    return ` CODEBLOCK${blocks.length - 1} `;
  });

  text = escapeHtml(text);
  text = text.replace(/`([^`\n]+)`/g, (m, code) => `<code>${code}</code>`);
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  const lines = text.split('\n');
  let html = '', inUl = false, inOl = false, para = [];
  const flushPara = () => { if (para.length) { html += `<p>${para.join('<br>')}</p>`; para = []; } };
  const closeLists = () => {
    if (inUl) { html += '</ul>'; inUl = false; }
    if (inOl) { html += '</ol>'; inOl = false; }
  };
  const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
  const splitRow = (line) => {
    let t = line.trim();
    if (t.startsWith('|')) t = t.slice(1);
    if (t.endsWith('|')) t = t.slice(0, -1);
    return t.split('|').map(c => c.trim());
  };
  const cellAlign = (raw) => {
    const t = (raw || '').trim();
    if (/^:-+:$/.test(t)) return 'center';
    if (/^-+:$/.test(t)) return 'right';
    if (/^:-+$/.test(t)) return 'left';
    return '';
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
      flushPara(); closeLists();
      const headers = splitRow(line);
      const aligns = splitRow(lines[i + 1]).map(cellAlign);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        rows.push(splitRow(lines[i]));
        i++;
      }
      const alignAttr = (idx) => aligns[idx] ? ` style="text-align:${aligns[idx]}"` : '';
      const thead = '<tr>' + headers.map((h, idx) => `<th${alignAttr(idx)}>${h}</th>`).join('') + '</tr>';
      const tbody = rows.map(r => '<tr>' + r.map((c, idx) => `<td${alignAttr(idx)}>${c || ''}</td>`).join('') + '</tr>').join('');
      html += `<div class="md-table-wrap"><table class="md-table">${thead}${tbody}</table></div>`;
      continue;
    }

    const cb = line.trim().match(/^CODEBLOCK(\d+)$/);
    if (cb) { flushPara(); closeLists(); html += ` CODEBLOCK${cb[1]} `; i++; continue; }
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    const ul = line.match(/^[-*]\s+(.*)$/);
    const ol = line.match(/^\d+\.\s+(.*)$/);
    const bq = line.match(/^>\s?(.*)$/);
    if (h) {
      flushPara(); closeLists();
      const n = h[1].length + 2; // h1/h2/h3 source -> h3/h4/h5, don't overpower a chat bubble
      html += `<h${n}>${h[2]}</h${n}>`;
    } else if (ul) {
      flushPara();
      if (inOl) { html += '</ol>'; inOl = false; }
      if (!inUl) { html += '<ul>'; inUl = true; }
      html += `<li>${ul[1]}</li>`;
    } else if (ol) {
      flushPara();
      if (inUl) { html += '</ul>'; inUl = false; }
      if (!inOl) { html += '<ol>'; inOl = true; }
      html += `<li>${ol[1]}</li>`;
    } else if (bq) {
      flushPara(); closeLists();
      html += `<blockquote>${bq[1]}</blockquote>`;
    } else if (line.trim() === '') {
      flushPara(); closeLists();
    } else {
      para.push(line);
    }
    i++;
  }
  flushPara(); closeLists();

  html = html.replace(/ CODEBLOCK(\d+) /g, (m, idx) => {
    const b = blocks[+idx];
    const code = highlightCode(b.code.replace(/\n$/, ''), b.lang);
    return `<div class="md-pre-wrap"><div class="md-pre-head"><span>${b.lang || ''}</span><button class="md-copy-btn" data-act="18">Copy</button></div><pre class="md-pre"><code>${code}</code></pre></div>`;
  });
  return html;
}

function copyCodeBlock(btn) {
  const code = btn.closest('.md-pre-wrap').querySelector('code').textContent;
  navigator.clipboard.writeText(code).then(() => {
    const orig = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = orig; }, 1200);
  }).catch(() => {});
}

function copyMessageText(btn) {
  const div = btn.closest('.msg');
  navigator.clipboard.writeText(div.dataset.rawText || '').then(() => {
    const orig = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = orig; }, 1200);
  }).catch(() => {});
}

async function regenerateMessage(btn) {
  if (!currentConvId) return;
  const div = btn.closest('.msg');
  const bubble = div.querySelector('.bubble');
  const who = div.querySelector('.who');
  const nameEl = who.querySelector('.msg-name') || who; // fallback for any pre-ts-rollout message still in the DOM
  const origName = nameEl.textContent;
  bubble.innerHTML = pendingDotsHtml();
  nameEl.textContent = APP_NAME;
  const thinkWrap = div.querySelector('.thinking-block');
  if (thinkWrap) thinkWrap.remove();
  try {
    const resp = await fetch(`/api/conversations/${currentConvId}/regenerate`, { method: 'POST' });
    const data = await resp.json();
    if (data.error) { bubble.textContent = 'Error: ' + data.error; nameEl.textContent = origName; return; }
    nameEl.textContent = data.model ? `${APP_NAME} · ${data.model}` : APP_NAME;
    if (data.ts) {
      let tsEl = who.querySelector('.msg-ts');
      if (!tsEl) {
        tsEl = document.createElement('span');
        tsEl.className = 'msg-ts';
        who.appendChild(tsEl);
      }
      tsEl.textContent = formatMsgTs(data.ts);
      tsEl.title = new Date(data.ts * 1000).toLocaleString();
      div.dataset.ts = data.ts;
    }
    div.dataset.rawText = data.reply;
    bubble.innerHTML = renderMarkdown(data.reply);
    if (data.thinking) div.insertBefore(buildThinkingBlock(data.thinking), div.firstChild.nextSibling);
  } catch (e) {
    bubble.textContent = 'Error: could not reach the local server';
    nameEl.textContent = origName;
  }
}

async function editMessage(btn) {
  if (!currentConvId) return;
  const div = btn.closest('.msg');
  const idx = Array.from(messagesEl.children).indexOf(div);
  if (idx < 0) return;
  const text = div.dataset.rawText || '';
  try {
    await fetch(`/api/conversations/${currentConvId}/truncate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index: idx }),
    });
  } catch (e) { return; }
  while (messagesEl.children.length > idx) messagesEl.removeChild(messagesEl.lastChild);
  if (messagesEl.children.length === 0) showHero();
  inputBox.value = text;
  autoResizeInput();
  inputBox.focus();
}

function pendingDotsHtml() {
  return 'Thinking <span class="pending-dots"><span></span><span></span><span></span></span>';
}

function buildThinkingBlock(thinking, label) {
  const wrap = document.createElement('div');
  const autoExpand = localStorage.getItem('jabber-autoexpand-thinking') === '1';
  wrap.className = 'thinking-block' + (autoExpand ? ' open' : '');
  const labelText = label || 'Thinking';
  wrap.innerHTML = `<button class="thinking-toggle" data-act="19"><span class="chev">▸</span> <span class="thinking-label">${labelText}</span></button><div class="thinking-body"></div>`;
  wrap.querySelector('.thinking-body').textContent = thinking;
  return wrap;
}

function formatMsgTs(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return sameDay ? time : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`;
}

function addMessage(who, role, text, model, thinking, images, ts) {
  hideHero();
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.dataset.rawText = text;
  const whoLabel = model ? `${who} · ${model}` : who;
  const whoEl = document.createElement('div');
  whoEl.className = 'who';
  const nameEl = document.createElement('span');
  nameEl.className = 'msg-name';
  nameEl.textContent = whoLabel;
  whoEl.appendChild(nameEl);
  if (ts) {
    const tsEl = document.createElement('span');
    tsEl.className = 'msg-ts';
    tsEl.textContent = formatMsgTs(ts);
    tsEl.title = new Date(ts * 1000).toLocaleString();
    whoEl.appendChild(tsEl);
    div.dataset.ts = ts;
  }
  div.appendChild(whoEl);
  if (role === 'assistant' && thinking) {
    div.appendChild(buildThinkingBlock(thinking));
  }
  if (images && images.length) {
    const imgWrap = document.createElement('div');
    imgWrap.className = 'msg-images';
    for (const im of images) {
      const mime = (im && im.mime) || 'image/png';
      const data = (im && im.data) || im; // tolerate a bare-string legacy shape too
      const img = document.createElement('img');
      img.src = `data:${mime};base64,${data}`;
      img.className = 'msg-image';
      img.addEventListener('click', () => window.open(img.src, '_blank'));
      imgWrap.appendChild(img);
    }
    div.appendChild(imgWrap);
  }
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (role === 'assistant') bubble.innerHTML = renderMarkdown(text);
  else bubble.textContent = text;
  div.appendChild(bubble);

  const actions = document.createElement('div');
  actions.className = 'msg-actions';
  const copyBtn = document.createElement('button');
  copyBtn.textContent = '⧉ Copy';
  copyBtn.addEventListener('click', () => copyMessageText(copyBtn));
  actions.appendChild(copyBtn);
  if (role === 'assistant') {
    const regenBtn = document.createElement('button');
    regenBtn.textContent = '↻ Regenerate';
    regenBtn.addEventListener('click', () => regenerateMessage(regenBtn));
    actions.appendChild(regenBtn);
  } else {
    const editBtn = document.createElement('button');
    editBtn.textContent = '✎ Edit';
    editBtn.addEventListener('click', () => editMessage(editBtn));
    actions.appendChild(editBtn);
  }
  div.appendChild(actions);

  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function showHero() {
  document.getElementById('hero').style.display = '';
  messagesEl.style.display = 'none';
}
function hideHero() {
  document.getElementById('hero').style.display = 'none';
  messagesEl.style.display = '';
}
function startChipPrompt(text) {
  inputBox.value = text;
  autoResizeInput();
  sendMessage();
}

async function refreshConvList() {
  try {
    const resp = await fetch('/api/conversations');
    const data = await resp.json();
    allConvs = data.conversations || [];
  } catch (e) {
    allConvs = [];
  }
  renderConvList();
}

function dateBucket(ts) {
  if (!ts) return 'Older';
  const now = new Date(), d = new Date(ts * 1000);
  const startOfDay = (dt) => new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
  const diffDays = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
  if (diffDays <= 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  if (diffDays <= 7) return 'This week';
  return 'Older';
}

function renderConvList() {
  const filter = (searchBox.value || '').toLowerCase();
  const filtered = allConvs.filter(c =>
    c.title.toLowerCase().includes(filter) &&
    (currentProjectId === null || c.project_id === currentProjectId)
  );
  convListEl.innerHTML = '';
  if (filtered.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'conv-group-label';
    empty.textContent = filter ? 'No matches' : 'No conversations yet';
    convListEl.appendChild(empty);
    return;
  }
  let lastBucket = null;
  for (const c of filtered) {
    const bucket = dateBucket(c.updated);
    if (bucket !== lastBucket) {
      const label = document.createElement('div');
      label.className = 'conv-group-label';
      label.textContent = bucket;
      convListEl.appendChild(label);
      lastBucket = bucket;
    }
    const div = document.createElement('div');
    div.className = 'conv' + (c.id === currentConvId ? ' active' : '');
    div.title = c.title;
    const titleSpan = document.createElement('span');
    titleSpan.className = 'convtitle';
    titleSpan.textContent = c.title;
    const renameBtn = document.createElement('button');
    renameBtn.className = 'convdel';
    renameBtn.textContent = '✎';
    renameBtn.title = 'Rename this conversation';
    renameBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const newTitle = prompt('Rename conversation:', c.title);
      if (!newTitle || !newTitle.trim() || newTitle.trim() === c.title) return;
      try {
        await fetch('/api/conversations/' + c.id + '/rename', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: newTitle.trim() }),
        });
      } catch (e) {}
      await refreshConvList();
    });
    const delBtn = document.createElement('button');
    delBtn.className = 'convdel';
    delBtn.textContent = '✕';
    delBtn.title = 'Delete this conversation';
    delBtn.addEventListener('click', async (e) => {
      e.stopPropagation(); // don't also trigger the row's own loadConversation
      if (!confirm(`Delete "${c.title}"? Cannot be undone.`)) return;
      try { await fetch('/api/conversations/' + c.id, { method: 'DELETE' }); } catch (e) {}
      if (c.id === currentConvId) { currentConvId = null; messagesEl.innerHTML = ''; showHero(); }
      await refreshConvList();
    });
    div.appendChild(titleSpan);
    div.appendChild(renameBtn);
    div.appendChild(delBtn);
    div.addEventListener('click', () => loadConversation(c.id));
    convListEl.appendChild(div);
  }
}

let pendingImages = [];

function renderPendingImages() {
  const wrap = document.getElementById('pendingImages');
  wrap.innerHTML = '';
  if (!pendingImages.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'flex';
  pendingImages.forEach((im, idx) => {
    const chip = document.createElement('div');
    chip.className = 'pending-image';
    const img = document.createElement('img');
    img.src = `data:${im.mime};base64,${im.data}`;
    chip.appendChild(img);
    const rm = document.createElement('button');
    rm.className = 'remove-btn';
    rm.textContent = '×';
    rm.title = 'Remove';
    rm.addEventListener('click', () => { pendingImages.splice(idx, 1); renderPendingImages(); });
    chip.appendChild(rm);
    wrap.appendChild(chip);
  });
}

function clearPendingImages() {
  pendingImages = [];
  renderPendingImages();
}

const MAX_CHAT_IMAGES_PER_MESSAGE = 4;

const DOWNSCALE_TARGET_PIXELS = 4_194_304; // 4MP, matches Ollama's real image_max_pixels
const DOWNSCALE_JPEG_QUALITY = 0.85;

async function downscaleImageIfNeeded(blob) {
  let bitmap;
  try {
    bitmap = await createImageBitmap(blob);
  } catch (e) {
    return blob;
  }
  const { width, height } = bitmap;
  const pixels = width * height;
  if (pixels <= DOWNSCALE_TARGET_PIXELS) {
    bitmap.close();
    return blob; // already under Ollama's own ceiling -- nothing to gain, don't re-encode a legit small image
  }
  const scale = Math.sqrt(DOWNSCALE_TARGET_PIXELS / pixels);
  const targetW = Math.max(1, Math.round(width * scale));
  const targetH = Math.max(1, Math.round(height * scale));
  const canvas = document.createElement('canvas');
  canvas.width = targetW;
  canvas.height = targetH;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(bitmap, 0, 0, targetW, targetH);
  bitmap.close();
  return new Promise((resolve) => {
    canvas.toBlob((outBlob) => resolve(outBlob || blob), 'image/jpeg', DOWNSCALE_JPEG_QUALITY);
  });
}

async function addPendingImageFromBlob(blob) {
  if (pendingImages.length >= MAX_CHAT_IMAGES_PER_MESSAGE) {
    const hint = document.getElementById('chatInputHint');
    const orig = hint.textContent;
    hint.textContent = `At most ${MAX_CHAT_IMAGES_PER_MESSAGE} images per message.`;
    setTimeout(() => { hint.textContent = orig; }, 4000);
    return;
  }
  blob = await downscaleImageIfNeeded(blob);
  const reader = new FileReader();
  reader.onload = () => {
    const commaIdx = reader.result.indexOf(',');
    const data = reader.result.slice(commaIdx + 1);
    pendingImages.push({ mime: blob.type || 'image/png', data });
    renderPendingImages();
  };
  reader.readAsDataURL(blob);
}

if (inputBox) {
  inputBox.addEventListener('paste', (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    let handledImage = false;
    for (const item of items) {
      if (item.type && item.type.startsWith('image/')) {
        const blob = item.getAsFile();
        if (blob) {
          addPendingImageFromBlob(blob);
          handledImage = true;
        }
      }
    }
    if (handledImage) e.preventDefault(); // don't also paste e.g. a filename/garbage text representation
  });
}

function webAvailable() { return !!(window.JABBER_FEATURES && window.JABBER_FEATURES.web); }
function webOn() { return webAvailable() && localStorage.getItem('jabber-web') === '1'; }
function initWebSetting() {
  const row = document.getElementById('webSettingRow'), box = document.getElementById('webSearchSetting');
  if (!row || !box) return;
  row.style.display = webAvailable() ? '' : 'none';
  box.checked = webOn();
  box.onchange = () => localStorage.setItem('jabber-web', box.checked ? '1' : '0');
}

function updateCtxMeter(c) {
  const el = document.getElementById('ctxMeter');
  if (!c || !c.limit) { el.hidden = true; return; }
  const k = (n) => n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n);
  let text = `Context ~${k(c.used)} / ${k(c.limit)}`;
  if (c.dropped > 0) {
    text += ` \u00b7 ${c.dropped} earlier message${c.dropped === 1 ? ' is' : 's are'} saved but no longer in the model's memory`;
  }
  el.textContent = text;
  el.classList.toggle('warn', c.dropped === 0 && c.used / c.limit > 0.6);
  el.classList.toggle('over', c.dropped > 0);
  el.hidden = false;
}

async function loadConversation(id) {
  if (isMobileView()) document.body.classList.add('sidebar-collapsed');
  try {
    const resp = await fetch('/api/conversations/' + id);
    if (!resp.ok) return;
    const conv = await resp.json();
    currentConvId = conv.id;
    currentProjectId = conv.project_id || null;
    updateCtxMeter(conv.context);
    messagesEl.innerHTML = '';
    if (conv.messages.length === 0) {
      showHero();
    } else {
      for (const m of conv.messages) {
        const el = addMessage(m.role === 'user' ? myDisplayName() : APP_NAME, m.role, m.content, null, m.thinking, m.images, m.ts);
        (m.tools || []).forEach((t) => addToolChip(el, t));
      }
    }
    renderConvList();
    renderProjList();
  } catch (e) {
  }
}

async function startNewChat() {
  updateCtxMeter(null);
  currentConvId = null;
  messagesEl.innerHTML = '';
  showHero();
  renderConvList();
  inputBox.focus();
}

async function sendMessage() {
  const text = inputBox.value.trim();
  if (!text && !pendingImages.length) return;
  inputBox.value = '';
  autoResizeInput();
  sendBtn.disabled = true;

  const modelSelect = document.getElementById('modelSelect');
  const model = modelSelect && modelSelect.value ? modelSelect.value : undefined;

  const imagesToSend = pendingImages.slice();
  clearPendingImages();

  addMessage(myDisplayName(), 'user', text, null, null, imagesToSend.length ? imagesToSend : undefined, Date.now() / 1000);
  const pending = addMessage(APP_NAME, 'assistant', '');
  pending.querySelector('.bubble').innerHTML = pendingDotsHtml();

  let thinkingEl = null, rawThinking = '', rawContent = '';
  let thinkingStartMs = null;
  try {
    if (!currentConvId) {
      const createResp = await fetch('/api/conversations', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(currentProjectId ? { project_id: currentProjectId } : {}),
      });
      const created = await createResp.json();
      currentConvId = created.id;
    }

    const chatBody = { message: text };
    if (webAvailable()) chatBody.web = webOn();
    if (model) chatBody.model = model;
    if (imagesToSend.length) chatBody.images = imagesToSend;
    const resp = await fetch(`/api/conversations/${currentConvId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(chatBody),
    });
    if (!resp.ok || !resp.body) {
      let errText = `HTTP ${resp.status}`;
      try { const d = await resp.json(); if (d.error) errText = d.error; } catch (e) {}
      pending.querySelector('.bubble').textContent = 'Error: ' + errText;
      sendBtn.disabled = false; inputBox.focus();
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '', sawContent = false;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        let evt;
        try { evt = JSON.parse(line); } catch (e) { continue; }

        if (evt.type === 'thinking_delta') {
          if (!thinkingEl) {
            thinkingStartMs = performance.now();
            thinkingEl = buildThinkingBlock('');
            thinkingEl.classList.add('open'); // always visible WHILE streaming, regardless of the Auto-expand default
            pending.insertBefore(thinkingEl, pending.querySelector('.bubble'));
          }
          rawThinking += evt.text;
          thinkingEl.querySelector('.thinking-body').textContent = rawThinking;
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (evt.type === 'content_delta') {
          if (!sawContent) {
            pending.querySelector('.bubble').innerHTML = '';
            sawContent = true;
            if (thinkingEl) {
              thinkingEl.classList.remove('open');
              if (thinkingStartMs !== null) {
                const secs = ((performance.now() - thinkingStartMs) / 1000).toFixed(1);
                const labelEl = thinkingEl.querySelector('.thinking-label');
                if (labelEl) labelEl.textContent = `Worked for ${secs}s`;
              }
            }
          }
          rawContent += evt.text;
          pending.dataset.rawText = rawContent;
          pending.querySelector('.bubble').innerHTML = renderMarkdown(rawContent);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (evt.type === 'note') {
          const n = document.createElement('div');
          n.className = 'tool-chips';
          n.innerHTML = '<span class="tool-chip"></span>';
          n.firstChild.textContent = '\u23f1 ' + evt.text;
          pending.querySelector('.bubble').before(n);
        } else if (evt.type === 'tool') {
          addToolChip(pending, evt);
        } else if (evt.type === 'done') {
          updateCtxMeter(evt.context);
          const whoEl = pending.querySelector('.who');
          const nameEl = whoEl.querySelector('.msg-name') || whoEl;
          nameEl.textContent = evt.model ? `${APP_NAME} · ${evt.model}` : APP_NAME;
          if (evt.ts) {
            let tsEl = whoEl.querySelector('.msg-ts');
            if (!tsEl) {
              tsEl = document.createElement('span');
              tsEl.className = 'msg-ts';
              whoEl.appendChild(tsEl);
            }
            tsEl.textContent = formatMsgTs(evt.ts);
            tsEl.title = new Date(evt.ts * 1000).toLocaleString();
            pending.dataset.ts = evt.ts;
          }
          if (thinkingEl && !sawContent) {
            thinkingEl.classList.remove('open');
            if (thinkingStartMs !== null) {
              const secs = ((performance.now() - thinkingStartMs) / 1000).toFixed(1);
              const labelEl = thinkingEl.querySelector('.thinking-label');
              if (labelEl) labelEl.textContent = `Worked for ${secs}s`;
            }
          }
          refreshConvList();
        } else if (evt.type === 'error') {
          pending.querySelector('.bubble').textContent = 'Error: ' + evt.error;
        }
      }
    }
    if (!sawContent) {
      const bubble = pending.querySelector('.bubble');
      if (bubble.innerHTML.includes('pending-dots')) bubble.textContent = '(empty reply)';
    }
  } catch (e) {
    pending.querySelector('.bubble').textContent = 'Error: could not reach the local server (' + e + ')';
  }
  sendBtn.disabled = false;
  inputBox.focus();
}

newChatBtn.addEventListener('click', startNewChat);
newProjectBtn.addEventListener('click', (e) => { e.stopPropagation(); createProject(); });
searchBox.addEventListener('input', renderConvList);

async function attachFilesToChat(fileList) {
  if (!fileList.length) return;
  const hint = document.getElementById('chatInputHint');
  const origHint = hint.textContent;
  let projId = currentProjectId;
  if (!projId) {
    hint.textContent = 'Creating a project for this upload…';
    try {
      const resp = await fetch('/api/projects', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: `Uploads ${new Date().toISOString().slice(0, 10)}` }),
      });
      const data = await resp.json();
      if (data.error) { hint.textContent = 'Error: ' + data.error; setTimeout(() => hint.textContent = origHint, 4000); return; }
      projId = data.id;
      currentProjectId = projId;
      await loadProjects();
      renderProjList();
      renderConvList();
    } catch (e) {
      hint.textContent = 'Could not reach the server.';
      setTimeout(() => hint.textContent = origHint, 4000);
      return;
    }
  }

  if (!currentConvId) {
    try {
      const createResp = await fetch('/api/conversations', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projId }),
      });
      const created = await createResp.json();
      if (created.error) {
        hint.textContent = 'Error starting chat: ' + created.error;
        setTimeout(() => hint.textContent = origHint, 5000);
        return;
      }
      currentConvId = created.id;
    } catch (e) {
      hint.textContent = 'Could not reach the server to start a chat.';
      setTimeout(() => hint.textContent = origHint, 5000);
      return;
    }
  } else {
    try {
      const linkResp = await fetch(`/api/conversations/${currentConvId}/set-project`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projId }),
      });
      const linkData = await linkResp.json();
      if (linkData.error) {
        hint.textContent = 'Error linking project to this chat: ' + linkData.error;
        setTimeout(() => hint.textContent = origHint, 5000);
        return;
      }
    } catch (e) {
      hint.textContent = 'Could not reach the server to link this chat to the project.';
      setTimeout(() => hint.textContent = origHint, 5000);
      return;
    }
  }

  hint.textContent = `Reading ${fileList.length} file(s)…`;
  const result = await readAndUploadFiles(projId, fileList, uploadProgressText(hint));
  if (!result.ok) {
    hint.textContent = result.note;
    setTimeout(() => { hint.textContent = origHint; }, 5000);
    return;
  }
  hint.textContent = uploadSummary(result);
  setTimeout(() => { hint.textContent = origHint; }, 9000);

  const filenames = result.writtenPaths && result.writtenPaths.length
    ? result.writtenPaths
    : Array.from(fileList).map(f => f.name);
  try {
    const noteResp = await fetch(`/api/conversations/${currentConvId}/attachment-note`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filenames }),
    });
    const noteData = await noteResp.json();
    if (!noteData.error) {
      const last = noteData.messages[noteData.messages.length - 1];
      addMessage(myDisplayName(), 'user', last.content, null, null, null, last.ts);
    }
  } catch (e) {
  }
}

const chatAttachBtn = document.getElementById('chatAttachBtn');
const chatAttachInput = document.getElementById('chatAttachInput');
if (chatAttachBtn && chatAttachInput) {
  chatAttachBtn.addEventListener('click', () => chatAttachInput.click());
  chatAttachInput.addEventListener('change', () => {
    const list = Array.from(chatAttachInput.files || []);
    chatAttachInput.value = ''; // reset so re-picking the same file(s) still fires 'change'
    attachFilesToChat(list);
  });
}

const chatDropZone = document.getElementById('chatDropZone');
let dragDepth = 0;
if (chatDropZone) {
  chatDropZone.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragDepth++;
    chatDropZone.classList.add('drag-over');
  });
  chatDropZone.addEventListener('dragover', (e) => e.preventDefault());
  chatDropZone.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) chatDropZone.classList.remove('drag-over');
  });
  chatDropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    chatDropZone.classList.remove('drag-over');
    const list = Array.from(e.dataTransfer?.files || []);
    if (list.length) attachFilesToChat(list);
  });
}

function addToolChip(msgEl, t) {
  if (!msgEl) return;
  let box = msgEl.querySelector('.tool-chips');
  if (!box) {
    box = document.createElement('div');
    box.className = 'tool-chips';
    msgEl.querySelector('.bubble').before(box);
  }
  const verbs = { crop_image: 'Zoomed into', image_metadata: 'Read metadata of', web_search: 'Searched the web for', fetch_page: 'Opened', list_project_files: 'Listed', read_project_file: 'Read', search_project_files: 'Searched for', read_project_image: 'Looked at' };
  const chip = document.createElement('span');
  chip.className = 'tool-chip';
  chip.textContent = '\ud83d\udcc4 ' + (verbs[t.name] || t.name) + ' ' + (t.label || '');
  box.appendChild(chip);
}

async function exportAllConversations() {
  const full = [];
  for (const c of allConvs) {
    try {
      const resp = await fetch('/api/conversations/' + c.id);
      if (resp.ok) full.push(await resp.json());
    } catch (e) { /* skip a conversation that failed to load, export the rest */ }
  }
  const blob = new Blob([JSON.stringify(full, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `jabber-conversations-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function clearAllConversations() {
  if (!confirm(`Delete all ${allConvs.length} conversation(s)? Cannot be undone.`)) return;
  for (const c of allConvs) {
    try { await fetch('/api/conversations/' + c.id, { method: 'DELETE' }); } catch (e) { /* best-effort */ }
  }
  currentConvId = null;
  messagesEl.innerHTML = '';
  showHero();
  await refreshConvList();
}

async function loadModelList() {
  const select = document.getElementById('modelSelect');
  if (!select) return;
  try {
    const resp = await fetch('/api/models');
    const data = await resp.json();
    if (data.error) {
      select.innerHTML = `<option>unavailable: ${data.error}</option>`;
      return;
    }
    select.innerHTML = data.models.map(m =>
      `<option value="${m}"${m === data.current ? ' selected' : ''}>${m}</option>`
    ).join('');
  } catch (e) {
    select.innerHTML = `<option>unavailable: ${e}</option>`;
  }
}

sendBtn.addEventListener('click', sendMessage);
inputBox.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

function autoResizeInput() {
  inputBox.style.height = 'auto';
  inputBox.style.height = Math.min(inputBox.scrollHeight, 140) + 'px';
}
inputBox.addEventListener('input', autoResizeInput);

const THEME_NAMES = {
  'moss-dark': 'Moss Dark',
  'moss-light': 'Moss Light',
  'slate-dark': 'Slate Dark',
  'slate-light': 'Slate Light',
  'standard-dark': 'Flexoki Dark',
  'standard-light': 'Flexoki Light',
  'cypress-dark': 'Cypress Dark',
  'cypress-light': 'Cypress Light',
};

function setTheme(id) {
  document.body.dataset.theme = id;
  document.querySelectorAll('.swatch').forEach(s => {
    s.classList.toggle('active', s.dataset.themeId === id);
  });
  const desc = document.getElementById('themeDesc');
  if (desc) desc.textContent = THEME_NAMES[id] || id;
  localStorage.setItem('jabber-theme', id);
  const btn = document.getElementById('themeToggleBtn');
  if (btn) btn.textContent = id === 'moss-light' ? '☼' : '☾';
}

function toggleThemeQuick() {
  const current = document.body.dataset.theme;
  setTheme(current === 'moss-light' ? 'moss-dark' : 'moss-light');
}

document.addEventListener('DOMContentLoaded', () => {
  const savedTheme = localStorage.getItem('jabber-theme');
  setTheme(THEME_NAMES[savedTheme] ? savedTheme : 'moss-dark');
  loadModelList();
  loadProjects();
  loadCurrentUser();
  refreshConvList();
  ['sidebar'].forEach(panel => {
    const saved = localStorage.getItem(`jabber-${panel}-collapsed`);
    const defaultCollapsed = panel === 'dash';
    const shouldCollapse = isMobileView() ? true : (saved !== null ? saved === '1' : defaultCollapsed);
    if (shouldCollapse) {
      document.body.classList.add(`${panel}-collapsed`);
    }
  });
  ['projects', 'history'].forEach(name => {
    if (localStorage.getItem(`jabber-section-${name}-collapsed`) === '1') {
      toggleSidebarSection(name);
    }
  });
  const autoExpandBox = document.getElementById('autoExpandThinking');
  if (autoExpandBox) {
    autoExpandBox.checked = localStorage.getItem('jabber-autoexpand-thinking') === '1';
    autoExpandBox.addEventListener('change', () => {
      localStorage.setItem('jabber-autoexpand-thinking', autoExpandBox.checked ? '1' : '0');
    });
  }
});

function toggleSidebarSection(name) {
  const header = document.getElementById(name === 'projects' ? 'projectsHeader' : 'historyHeader');
  const body = name === 'projects' ? projListEl : convListEl;
  const collapsed = header.classList.toggle('collapsed');
  body.classList.toggle('collapsed', collapsed);
  localStorage.setItem(`jabber-section-${name}-collapsed`, collapsed ? '1' : '0');
}

function isMobileView() { return window.matchMedia('(max-width: 760px)').matches; }
function togglePanel(panel) {
  const collapsed = document.body.classList.toggle(`${panel}-collapsed`);
  if (!isMobileView()) localStorage.setItem(`jabber-${panel}-collapsed`, collapsed ? '1' : '0');
}
document.addEventListener('click', (e) => {
  if (!isMobileView() || document.body.classList.contains('sidebar-collapsed')) return;
  if (e.target.closest('#sidebarToggle')) return;
  if (!e.target.closest('.sidebar') || e.target.closest('.conv-item, .conv, [data-conv-id]')) document.body.classList.add('sidebar-collapsed');
});
