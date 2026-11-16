// SoC Print front end. Plain DOM, no build step.

const TOKEN = document.querySelector('meta[name="soc-print-token"]').content;
const $ = (id) => document.getElementById(id);

const model = {
  auth: {},
  settings: {},
  printers: [],
  accessLabels: {},
  servers: [],
  tools: {},
  files: [], // entries known to the server
  uploads: [], // rows still uploading, or that failed to convert
  pages: new Map(), // file id -> page range text
  printErrors: new Map(), // file id -> message from a failed lpr
  copies: 1,
  plan: null,
  sending: false,
  jobsQueue: '',
};

// ------------------------------------------------------------------ helpers

class ApiError extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

async function api(method, path, body) {
  const init = { method, headers: { 'X-SOC-Print-Token': TOKEN } };
  if (body instanceof Blob) {
    init.body = body;
    init.headers['Content-Type'] = 'application/octet-stream';
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers['Content-Type'] = 'application/json';
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError('offline', 'SoC Print has stopped. Start it again with ./soc-print, then reload.');
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    // handled below
  }
  if (!res.ok || data === null) {
    const err = data?.error;
    throw new ApiError(err?.kind || 'internal', err?.message || `Request failed (${res.status})`);
  }
  return data;
}

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === false || value == null) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, value);
  }
  el.append(...children.filter((c) => c != null && c !== false));
  return el;
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function formatTime(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function toast(message, tone = '') {
  const el = h('div', { class: `toast ${tone}`, text: message });
  $('toasts').append(el);
  setTimeout(() => el.remove(), tone === 'bad' ? 10000 : 5000);
}

function report(err) {
  if (err.kind === 'auth' || err.kind === 'rejected') openAccount(err.message);
  else toast(err.message, 'bad');
}

const activeFiles = () => model.files.filter((f) => !f.sent);
const currentPrinter = () => model.printers.find((p) => p.id === model.settings.printer);

// ------------------------------------------------------------------- state

function applyState(state) {
  model.auth = state.auth;
  model.printers = state.printers;
  model.accessLabels = state.accessLabels;
  model.servers = state.servers;
  model.tools = state.tools;
  model.files = state.files;
  if (!model.settings.printer) {
    model.settings = { ...state.settings };
    buildPrinterList();
  }
  model.connection = { host: state.settings.host, route: state.settings.route, remember: state.settings.remember };
  renderAccount();
  renderSettings();
  renderFiles();
  renderSummary();
}

async function reloadState() {
  applyState(await api('GET', '/api/state'));
}

// ----------------------------------------------------------------- account

function renderAccount() {
  const { username, connected, ready } = model.auth;
  $('account-dot').className = `dot ${connected ? 'on' : ready ? 'idle' : ''}`;
  $('account-label').textContent = ready && username ? username : 'Sign in';
  $('account').title = connected ? 'Connected' : ready ? 'Signed in; connects when needed' : 'Not signed in';
}

function openAccount(message = '') {
  const { auth, connection } = model;
  const dialog = $('signin');
  $('signin-title').textContent = auth.ready ? 'SoC account' : 'Sign in to SoC';
  if (auth.connected) {
    const via = auth.route === 'jump' ? 'through the jump host' : 'directly';
    const saved = auth.saved ? ' The password is saved in your system keyring.' : '';
    $('signin-status').textContent = `Connected to ${connection.host} ${via} as ${auth.username}.${saved}`;
  } else {
    $('signin-status').replaceChildren(
      'Use your SoC account, not NUSNET. It is used to run ',
      h('code', { text: 'lpr' }),
      ' on the SoC login server over SSH.',
    );
  }
  $('si-user').value = auth.username || '';
  $('si-pass').value = '';
  $('si-pass').placeholder = auth.saved ? 'Saved. Type to replace it' : '';
  $('si-remember').checked = auth.keyring && connection.remember;
  $('si-remember').disabled = !auth.keyring;
  $('si-remember-note').textContent = auth.keyring
    ? `Saves the password in your system keyring (${auth.keyringName}), not in a file.`
    : 'No system keyring found, so you will sign in each time SoC Print starts.';
  $('si-host').replaceChildren(
    ...model.servers.map((s) => h('option', { value: s.host, text: s.label, selected: s.host === connection.host })),
  );
  $('si-route').value = connection.route;
  $('si-signout').hidden = !auth.ready;
  $('si-submit').textContent = auth.ready ? 'Update' : 'Sign in';
  showSignInError(message || auth.message);
  if (!dialog.open) dialog.showModal();
  (auth.username ? $('si-pass') : $('si-user')).focus();
}

function showSignInError(message) {
  $('si-error').textContent = message || '';
  $('si-error').hidden = !message;
}

async function submitSignIn(event) {
  event.preventDefault();
  const button = $('si-submit');
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Connecting…';
  showSignInError('');
  try {
    const state = await api('POST', '/api/signin', {
      username: $('si-user').value,
      password: $('si-pass').value,
      remember: $('si-remember').checked,
      host: $('si-host').value,
      route: $('si-route').value,
    });
    applyState(state);
    $('si-pass').value = '';
    $('signin').close();
    toast(`Signed in as ${state.auth.username}`);
    refreshJobs();
    refreshQuota();
  } catch (err) {
    showSignInError(err.message);
    $('si-pass').select();
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function signOut() {
  try {
    applyState(await api('POST', '/api/signout'));
    $('signin').close();
    $('jobs').replaceChildren('Sign in to see the queue.');
    $('quota').replaceChildren('Sign in to see your quota.');
    toast('Signed out. The saved password was removed.');
  } catch (err) {
    showSignInError(err.message);
  }
}

// ---------------------------------------------------------------- settings

function buildPrinterList() {
  const groups = Object.entries(model.accessLabels).map(([access, label]) =>
    h('optgroup', { label }, ...model.printers
      .filter((p) => p.access === access)
      .map((p) => {
        const traits = [p.kind === 'colour' ? 'Colour' : null, p.paper !== 'A4' ? p.paper : null].filter(Boolean);
        const where = p.location.split(',').slice(0, 2).join(',');
        return h('option', { value: p.id, text: [p.id, ...traits, where].join(' · ') });
      })));
  groups.push(h('optgroup', { label: 'Other' }, h('option', { value: 'custom', text: 'Another queue…' })));
  $('printer').replaceChildren(...groups);
}

// Keep sides and banner within what the chosen printer's queues can do.
function normaliseSettings(changed) {
  const s = model.settings;
  const printer = currentPrinter();
  if (!printer) return;
  const q = printer.queues;
  if (!q[s.sides]) s.sides = q.duplex ? 'duplex' : 'simplex';
  if (!printer.banner || s.banner || q[`${s.sides}-nb`]) return;
  const other = s.sides === 'duplex' ? 'simplex' : 'duplex';
  if (changed === 'banner' && q[`${other}-nb`]) s.sides = other;
  else s.banner = true;
}

function renderSettings() {
  const s = model.settings;
  const printer = currentPrinter();
  const custom = s.printer === 'custom';
  $('printer').value = s.printer;
  $('custom-queue').hidden = !custom;
  if (custom && $('custom-queue').value !== s.queue) $('custom-queue').value = s.queue || '';

  const q = printer?.queues || {};
  for (const input of document.querySelectorAll('input[name="sides"]')) {
    input.checked = input.value === s.sides;
    input.disabled = custom || !q[input.value];
  }
  for (const input of document.querySelectorAll('input[name="nup"]')) {
    input.checked = Number(input.value) === s.nup;
  }
  $('copies').value = model.copies;

  const canSkipBanner = Boolean(q['duplex-nb'] || q['simplex-nb']);
  $('banner-row').hidden = custom || !printer?.banner;
  $('banner').checked = s.banner;
  $('banner').disabled = !canSkipBanner;
  $('banner-row').classList.toggle('disabled', !canSkipBanner);
  $('fast').checked = s.fastHeavy;

  const notes = [];
  if (custom) {
    notes.push('Sides and banner follow the queue name: -sx single-sided, -dx double-sided, -nb no banner.');
  } else if (printer) {
    notes.push(printer.location + '.');
    notes.push(`${printer.model}, ${printer.kind === 'colour' ? 'colour' : 'black and white'} ${printer.paper}.`);
    if (printer.access !== 'open') notes.push(`${model.accessLabels[printer.access]}.`);
    if (printer.banner && !canSkipBanner) notes.push('Always prints a banner page.');
    else if (printer.banner && !s.banner && !q['simplex-nb']) notes.push('Without a banner it prints double-sided only.');
    if (!printer.banner && printer.kind === 'bw') notes.push('No banner page.');
  }
  $('printer-note').textContent = notes.join(' ');
}

const saveSettings = debounce(() => {
  const { printer, queue, sides, banner, nup, fastHeavy } = model.settings;
  api('POST', '/api/settings', { printer, queue, sides, banner, nup, fastHeavy }).catch(() => {});
}, 400);

function settingsChanged(what) {
  normaliseSettings(what);
  renderSettings();
  saveSettings();
  schedulePlan();
}

// ------------------------------------------------------------------- files

function captureFocus() {
  const el = document.activeElement;
  if (!el?.dataset?.pagesFor) return null;
  return { id: el.dataset.pagesFor, start: el.selectionStart, end: el.selectionEnd };
}

function restoreFocus(saved) {
  if (!saved) return;
  const el = document.querySelector(`[data-pages-for="${saved.id}"]`);
  if (!el) return;
  el.focus();
  el.setSelectionRange(saved.start, saved.end);
}

function fileRow(file) {
  const meta = [plural(file.pages, 'page'), formatSize(file.size)];
  if (file.kind !== 'pdf') meta.push(`${file.kindLabel} converted to PDF`);
  const heavy = file.heavyPages
    ? h('span', { class: 'heavy', title: 'Large photos or transparency effects: slow for the printer to process',
      text: ` · ${plural(file.heavyPages, 'heavy page')}` })
    : null;
  return h('li', { class: 'file', dataset: { id: file.id } },
    h('div', { class: 'badge', text: badgeText(file) }),
    h('div', { class: 'file-main' },
      h('div', { class: 'file-name', text: file.name, title: file.name }),
      h('div', { class: 'file-meta' }, meta.join(' · '), heavy)),
    h('div', { class: 'file-actions' },
      h('input', {
        class: 'pages', type: 'text', placeholder: 'All pages', value: model.pages.get(file.id) || '',
        'aria-label': `Pages of ${file.name} to print, for example 1-3, 7`,
        autocomplete: 'off', spellcheck: 'false', dataset: { pagesFor: file.id },
        oninput: (e) => {
          model.pages.set(file.id, e.target.value);
          model.printErrors.delete(file.id);
          schedulePlan();
        },
      }),
      h('button', { class: 'ghost small', type: 'button', text: 'Preview', onclick: () => preview(file) }),
      h('button', { class: 'icon-btn', type: 'button', text: '×', 'aria-label': `Remove ${file.name}`, onclick: () => removeFile(file) })),
    h('div', { class: 'file-error', hidden: true }));
}

function badgeText(file) {
  const ext = file.name.includes('.') ? file.name.split('.').pop() : '';
  return (ext || file.kind).slice(0, 4).toUpperCase();
}

function uploadRow(upload) {
  if (upload.error) {
    return h('li', { class: 'file failed' },
      h('div', { class: 'badge', text: '!' }),
      h('div', { class: 'file-main' },
        h('div', { class: 'file-name', text: upload.name, title: upload.name }),
        h('div', { class: 'file-error', text: upload.error })),
      h('div', { class: 'file-actions' },
        h('button', { class: 'icon-btn', type: 'button', text: '×', 'aria-label': `Dismiss ${upload.name}`,
          onclick: () => { model.uploads = model.uploads.filter((u) => u !== upload); renderFiles(); } })));
  }
  return h('li', { class: 'file' },
    h('div', { class: 'badge' }, h('div', { class: 'spinner' })),
    h('div', { class: 'file-main' },
      h('div', { class: 'file-name', text: upload.name, title: upload.name }),
      h('div', { class: 'file-meta', text: `Getting it ready to print · ${formatSize(upload.size)}` })),
    h('div'));
}

function sentRow(file) {
  const { queue, sheets, at, bytes, rasterized } = file.sent;
  const details = [`Sent to ${queue} at ${formatTime(at)}`, plural(sheets, 'sheet')];
  if (bytes) details.push(formatSize(bytes));
  if (rasterized) details.push(`${plural(rasterized, 'heavy printed page')} sped up`);
  return h('li', { class: 'file', dataset: { id: file.id } },
    h('div', { class: 'badge sent', text: '✓', 'aria-hidden': 'true' }),
    h('div', { class: 'file-main' },
      h('div', { class: 'file-name', text: file.name, title: file.name }),
      h('div', { class: 'file-meta', text: details.join(' · ') })),
    h('div', { class: 'file-actions' },
      h('button', { class: 'ghost small', type: 'button', text: 'Print again', onclick: () => printAgain(file) }),
      h('button', { class: 'icon-btn', type: 'button', text: '×', 'aria-label': `Remove ${file.name}`, onclick: () => removeFile(file) })));
}

function renderFiles() {
  const focus = captureFocus();
  const active = activeFiles();
  const sent = model.files.filter((f) => f.sent);
  $('files').replaceChildren(...active.map(fileRow), ...model.uploads.map(uploadRow));
  $('sent').replaceChildren(...sent.map(sentRow));
  $('sent-section').hidden = sent.length === 0;
  $('drop').classList.toggle('compact', model.files.length + model.uploads.length > 0);
  $('drop-hint').textContent = model.tools.office === false
    ? 'or click to choose, or paste. PDF and images work now; install LibreOffice for Word, PowerPoint, Excel and text.'
    : 'or click to choose, or paste. PDF, Word, PowerPoint, Excel, images and text.';
  restoreFocus(focus);
  markRows();
}

// Show per-file problems (bad page range, failed lpr) without rebuilding the rows.
function markRows() {
  for (const row of $('files').querySelectorAll('.file[data-id]')) {
    const id = row.dataset.id;
    const planError = model.plan?.files?.[id]?.error;
    const message = planError || model.printErrors.get(id) || '';
    const box = row.querySelector('.file-error');
    box.textContent = message;
    box.hidden = !message;
    row.querySelector('.pages').classList.toggle('invalid', Boolean(planError));
    row.classList.toggle('failed', Boolean(message));
  }
}

async function addFiles(fileList) {
  const picked = [...fileList];
  if (!picked.length) return;
  const uploads = picked.map((file) => ({ file, name: file.name, size: file.size, error: '' }));
  model.uploads.push(...uploads);
  renderFiles();
  await Promise.all(uploads.map(async (upload) => {
    try {
      const entry = await api('POST', `/api/files?name=${encodeURIComponent(upload.name)}`, upload.file);
      if (entry.error) {
        upload.error = entry.error;
      } else {
        model.uploads = model.uploads.filter((u) => u !== upload);
        model.files.push(entry);
      }
    } catch (err) {
      upload.error = err.message;
    }
    renderFiles();
    schedulePlan();
  }));
}

async function removeFile(file) {
  model.files = model.files.filter((f) => f.id !== file.id);
  model.pages.delete(file.id);
  model.printErrors.delete(file.id);
  renderFiles();
  schedulePlan();
  api('DELETE', `/api/files/${file.id}`).catch(() => {});
}

async function printAgain(file) {
  try {
    await api('POST', `/api/files/${file.id}/reactivate`);
    file.sent = null;
    renderFiles();
    schedulePlan();
  } catch (err) {
    report(err);
  }
}

async function preview(file) {
  // Open the tab during the click so the browser doesn't treat it as a pop-up.
  const tab = window.open('', '_blank');
  try {
    const { url, rasterized, note } = await api('POST', '/api/preview', { ...jobRequest([]), id: file.id, pages: model.pages.get(file.id) || '' });
    if (rasterized) toast(`Preview shows ${plural(rasterized, 'heavy printed page')} as the images that will be printed`);
    if (note) toast(note, 'bad');
    if (tab) tab.location.replace(url);
    else window.location.assign(url);
  } catch (err) {
    tab?.close();
    report(err);
  }
}

// -------------------------------------------------------- plan and printing

function jobRequest(files) {
  const { printer, queue, sides, banner, nup, fastHeavy } = model.settings;
  return {
    printer, queue, sides, banner, nup, fastHeavy,
    copies: model.copies,
    files: files.map((f) => ({ id: f.id, pages: model.pages.get(f.id) || '' })),
  };
}

let planSeq = 0;
async function refreshPlan() {
  const seq = ++planSeq;
  let plan;
  try {
    plan = await api('POST', '/api/plan', jobRequest(activeFiles()));
  } catch (err) {
    plan = { ok: false, error: err.message, files: {} };
  }
  if (seq !== planSeq) return;
  model.plan = plan;
  renderSummary();
  markRows();
  if (plan.queue && plan.queue !== model.jobsQueue) {
    model.jobsQueue = plan.queue;
    scheduleJobs();
  }
}
const schedulePlan = debounce(refreshPlan, 120);

function renderSummary() {
  const { plan, auth, sending } = model;
  const count = activeFiles().length;
  const box = $('summary');
  const button = $('print');
  const waiting = model.uploads.some((u) => !u.error);

  if (!plan) {
    box.replaceChildren();
  } else if (plan.error) {
    box.replaceChildren(h('span', { class: 'bad', text: plan.error }));
  } else {
    const t = plan.totals;
    const where = [plan.queue, plan.location, plan.duplex ? 'double-sided' : 'single-sided'].filter(Boolean).join(' · ');
    const lines = [];
    if (t.files) {
      lines.push(h('strong', { text: `${plural(t.pages, 'page')} on ${plural(t.sheets, 'sheet')} of ${plan.paper}` }));
      lines.push(h('div', { text: where }));
      lines.push(h('div', { text: `About ${t.quota} from your ${plan.kind === 'colour' ? 'colour' : 'B/W'} quota` }));
      if (t.heavy) lines.push(h('div', { class: 'heavy', text: heavyLine(plan) }));
    } else {
      lines.push(h('div', { text: where }));
    }
    if (!plan.ok) lines.push(h('div', { class: 'bad', text: 'Fix the page range marked above.' }));
    box.replaceChildren(...lines);
  }

  if (sending) {
    button.textContent = plan?.fastHeavy && plan?.totals?.heavy ? 'Speeding up heavy pages…' : 'Sending…';
    button.disabled = true;
  } else if (!auth.ready) {
    button.textContent = 'Sign in to print';
    button.disabled = false;
  } else {
    button.textContent = count ? `Print ${plural(count, 'file')}` : 'Print';
    button.disabled = !count || !plan?.ok || waiting;
  }
}

function heavyLine(plan) {
  const n = plural(plan.totals.heavy, 'heavy printed page');
  if (!plan.fastHeavy) return `${n} sent unchanged; the printer may take a while on them`;
  if (!plan.canSpeedUp) return `${n}: install Ghostscript to speed them up`;
  return `${n} will print as images, much faster`;
}

const QUOTA_HINT = /quota|insufficient|exceed/i;

function refusal(message) {
  const text = `The print server refused this job: ${message}`;
  return QUOTA_HINT.test(message) ? `${text} You may need to top up your quota (link under Print quota).` : text;
}

async function printAll() {
  if (!model.auth.ready) return openAccount();
  const files = activeFiles();
  if (!files.length || !model.plan?.ok || model.sending) return;
  model.sending = true;
  renderSummary();
  try {
    const res = await api('POST', '/api/print', jobRequest(files));
    let sent = 0;
    for (const result of res.results) {
      if (result.ok) {
        sent += 1;
        model.printErrors.delete(result.id);
      } else {
        model.printErrors.set(result.id, refusal(result.message));
      }
    }
    if (sent) {
      const faster = res.results.reduce((n, r) => n + (r.ok ? r.rasterized || 0 : 0), 0);
      toast(`Sent ${plural(sent, 'file')} to ${res.queue}` + (faster ? `, ${plural(faster, 'heavy printed page')} sped up` : ''));
    }
    for (const note of new Set(res.results.map((r) => r.note).filter(Boolean))) toast(note, 'bad');
    if (res.error) report(new ApiError(res.error.kind, res.error.message));
  } catch (err) {
    report(err);
  } finally {
    model.sending = false;
    await reloadState().catch(report);
    schedulePlan();
    refreshJobs();
    refreshQuota();
  }
}

// --------------------------------------------------------- queue and quota

async function withSpinner(buttonId, task) {
  const button = $(buttonId);
  button.classList.add('busy');
  try {
    await task();
  } finally {
    button.classList.remove('busy');
  }
}

function panelError(el, err, signedOutText) {
  if (err.kind === 'auth' || err.kind === 'rejected') {
    el.replaceChildren(signedOutText);
    reloadState().catch(() => {});
  } else {
    el.replaceChildren(h('span', { text: err.message }));
  }
}

async function refreshJobs() {
  const queue = model.jobsQueue;
  const tag = $('jobs-queue');
  tag.textContent = queue;
  tag.hidden = !queue;
  if (!queue || !model.auth.ready) return;
  await withSpinner('jobs-refresh', async () => {
    try {
      const res = await api('GET', `/api/jobs?queue=${encodeURIComponent(queue)}`);
      if (queue !== model.jobsQueue) return;
      renderJobs(res);
      if (!model.auth.connected) reloadState().catch(() => {});
    } catch (err) {
      panelError($('jobs'), err, 'Sign in to see the queue.');
    }
  });
}
const scheduleJobs = debounce(refreshJobs, 500);

function renderJobs(res) {
  const box = $('jobs');
  const raw = res.raw || 'The queue is empty.';
  if (!res.jobs.length) {
    box.replaceChildren(h('pre', { text: raw }));
    return;
  }
  const rows = res.jobs.map((job) => h('li', { class: `job ${job.mine ? 'mine' : ''}` },
    h('div', { class: 'job-main' },
      h('div', { class: 'job-title', text: `#${job.job} · ${job.rank} · ${job.owner.split('@')[0]}` }),
      h('div', { class: 'job-detail', text: job.detail, title: job.detail })),
    job.mine && h('button', { class: 'ghost small danger', type: 'button', text: 'Cancel', onclick: () => cancelJob(res.queue, job.job) })));
  box.replaceChildren(
    h('ul', { class: 'jobs' }, ...rows),
    h('details', {}, h('summary', { text: 'Raw lpq output' }), h('pre', { text: raw })));
}

async function cancelJob(queue, job) {
  try {
    const res = await api('POST', '/api/jobs/cancel', { queue, job });
    toast(res.ok ? `Asked ${queue} to cancel job ${job}` : res.message || 'Could not cancel that job', res.ok ? '' : 'bad');
  } catch (err) {
    report(err);
  }
  refreshJobs();
}

async function refreshQuota() {
  if (!model.auth.ready) return;
  await withSpinner('quota-refresh', async () => {
    try {
      const res = await api('GET', '/api/quota');
      $('quota').replaceChildren(h('pre', { text: res.text || 'No quota information returned.' }));
    } catch (err) {
      panelError($('quota'), err, 'Sign in to see your quota.');
    }
  });
}

// ------------------------------------------------------------------ wiring

function filesFromDrop(transfer) {
  const out = [];
  for (const item of transfer.items || []) {
    if (item.kind !== 'file') continue;
    if (item.webkitGetAsEntry?.()?.isDirectory) continue;
    const file = item.getAsFile();
    if (file) out.push(file);
  }
  return out.length ? out : [...(transfer.files || [])];
}

function wire() {
  const drop = $('drop');
  const picker = $('picker');
  drop.addEventListener('click', () => picker.click());
  drop.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      picker.click();
    }
  });
  picker.addEventListener('change', () => {
    addFiles(picker.files);
    picker.value = '';
  });

  // Accept drops anywhere on the page.
  const carriesFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  let depth = 0;
  window.addEventListener('dragenter', (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    depth += 1;
    $('veil').hidden = false;
  });
  window.addEventListener('dragover', (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  window.addEventListener('dragleave', (e) => {
    if (!carriesFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) $('veil').hidden = true;
  });
  window.addEventListener('drop', (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    depth = 0;
    $('veil').hidden = true;
    addFiles(filesFromDrop(e.dataTransfer));
  });
  window.addEventListener('paste', (e) => {
    if (e.target.closest?.('input, textarea, select')) return;
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;
    e.preventDefault();
    addFiles(files);
  });

  $('printer').addEventListener('change', (e) => {
    model.settings.printer = e.target.value;
    settingsChanged('printer');
    if (e.target.value === 'custom') $('custom-queue').focus();
  });
  $('custom-queue').addEventListener('input', (e) => {
    model.settings.queue = e.target.value.trim().toLowerCase();
    saveSettings();
    schedulePlan();
  });
  $('sides').addEventListener('change', (e) => {
    model.settings.sides = e.target.value;
    settingsChanged('sides');
  });
  $('nup').addEventListener('change', (e) => {
    model.settings.nup = Number(e.target.value);
    settingsChanged('nup');
  });
  $('banner').addEventListener('change', (e) => {
    model.settings.banner = e.target.checked;
    settingsChanged('banner');
  });
  $('fast').addEventListener('change', (e) => {
    model.settings.fastHeavy = e.target.checked;
    settingsChanged('fast');
  });

  const setCopies = (n) => {
    model.copies = Math.min(50, Math.max(1, Math.round(Number(n)) || 1));
    $('copies').value = model.copies;
    schedulePlan();
  };
  $('copies').addEventListener('change', (e) => setCopies(e.target.value));
  $('copies-less').addEventListener('click', () => setCopies(model.copies - 1));
  $('copies-more').addEventListener('click', () => setCopies(model.copies + 1));

  $('print').addEventListener('click', printAll);
  $('jobs-refresh').addEventListener('click', refreshJobs);
  $('quota-refresh').addEventListener('click', refreshQuota);

  $('account').addEventListener('click', () => openAccount());
  $('signin-form').addEventListener('submit', submitSignIn);
  $('si-cancel').addEventListener('click', () => $('signin').close());
  $('si-signout').addEventListener('click', signOut);
}

async function start() {
  wire();
  try {
    await reloadState();
  } catch (err) {
    toast(err.message, 'bad');
    return;
  }
  normaliseSettings();
  renderSettings();
  await refreshPlan();
  if (model.auth.ready) refreshQuota();
  else openAccount();
}

start();
