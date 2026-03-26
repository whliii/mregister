const APP_CONFIG = window.__APP_CONFIG__ || {};
const T = APP_CONFIG.translations || {};

const state = {
  credentials: [],
  emailCredentialStats: [],
  proxies: [],
  subscriptions: [],
  proxyNodes: [],
  taskTemplates: [],
  tasks: [],
  apiKeys: [],
  defaults: {},
  dashboard: {},
  platforms: {},
  proxyFeedback: {},
  nodeFeedback: {},
  selectedTemplateId: null,
  selectedTaskId: null,
  taskFilterStatus: 'all',
};

const sections = Array.from(document.querySelectorAll('.section-card'));
const navButtons = Array.from(document.querySelectorAll('.nav-btn'));
const appShell = document.getElementById('app-shell');
const sectionIndicator = document.getElementById('section-indicator');
const sidebarToggle = document.getElementById('sidebar-toggle');
const mobileNavButton = document.getElementById('mobile-nav-btn');
const sidebarOverlay = document.getElementById('sidebar-overlay');
const SIDEBAR_STORAGE_KEY = 'mregister-sidebar-collapsed';

function tr(key, vars = {}) {
  let value = T[key] || key;
  Object.entries(vars).forEach(([name, replacement]) => {
    value = value.replaceAll(`{${name}}`, String(replacement));
  });
  return value;
}

function statusLabel(status) {
  return T[`status_${status}`] || status;
}

function formatProbeSummary(item) {
  if (!item) {
    return tr('proxy_not_tested');
  }
  const prefix = item.has_local_snapshot ? `${tr('proxy_local_snapshot')} | ` : '';
  if (item.last_probe_status === 'success' && item.last_probe_at) {
    return `${prefix}${tr('proxy_last_probe_success', { time: item.last_probe_at })} | ${item.last_probe_latency ?? '-'} ms`;
  }
  if (item.last_probe_status === 'failed' && item.last_probe_at) {
    return `${prefix}${tr('proxy_last_probe_fail', { time: item.last_probe_at })}${item.cooldown_until ? ` | ${tr('proxy_cooldown_until', { time: item.cooldown_until })}` : ''}`;
  }
  if (item.cooldown_until) {
    return `${prefix}${tr('proxy_cooldown_until', { time: item.cooldown_until })}`;
  }
  return `${prefix}${tr('proxy_not_tested')}`;
}

function formatNodeProbeSummary(node) {
  const feedback = state.nodeFeedback[node?.id];
  if (feedback?.summary) {
    return feedback.summary;
  }
  if (node?.last_latency || node?.last_latency === 0) {
    return `${node.last_latency} ms`;
  }
  return tr('node_latency_unknown');
}

function formatProxyFeedback(item) {
  const feedback = state.proxyFeedback[item?.id];
  return feedback?.message || '';
}

function formatProxySnapshot(item) {
  if (!item?.has_local_snapshot) {
    return '';
  }
  return tr('proxy_snapshot_source', {
    name: item.snapshot_name || '-',
    protocol: item.snapshot_protocol || '-',
    server: item.snapshot_server || '-',
    port: item.snapshot_port || '-',
  });
}

function formatProxyStatus(item) {
  if (item?.cooldown_until) {
    return `${tr('proxy_status_cooling')} | ${tr('proxy_cooldown_until', { time: item.cooldown_until })}`;
  }
  if (item?.last_probe_status === 'success') {
    return `${tr('proxy_status_success')}${item.last_probe_latency || item.last_probe_latency === 0 ? ` | ${item.last_probe_latency} ms` : ''}`;
  }
  if (item?.last_probe_status === 'failed') {
    return tr('proxy_status_failed');
  }
  return tr('proxy_not_tested');
}

function appendTextLine(container, text) {
  if (!container || !text) {
    return;
  }
  const line = document.createElement('div');
  line.className = 'entity-note-line';
  line.textContent = text;
  container.appendChild(line);
}

function getEmailCredentialStatsForCredential(credentialId) {
  return (state.emailCredentialStats || [])
    .filter((item) => Number(item.credential_id) === Number(credentialId))
    .sort((left, right) => String(left.platform || '').localeCompare(String(right.platform || '')));
}

function isMobileLayout() {
  return window.matchMedia('(max-width: 960px)').matches;
}

function setSidebarCollapsed(collapsed) {
  if (!appShell) {
    return;
  }
  appShell.classList.toggle('sidebar-collapsed', collapsed);
  window.localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? '1' : '0');
}

function setSidebarOpen(open) {
  if (!appShell) {
    return;
  }
  appShell.classList.toggle('sidebar-open', open);
}

function closeMobileSidebar() {
  if (isMobileLayout()) {
    setSidebarOpen(false);
  }
}

function syncSectionIndicator(button) {
  if (!sectionIndicator || !button) {
    return;
  }
  sectionIndicator.textContent = button.dataset.label || button.textContent.trim();
}

function showSection(sectionId) {
  sections.forEach((section) => {
    section.classList.toggle('active', section.id === `section-${sectionId}`);
  });
  navButtons.forEach((button) => {
    const active = button.dataset.section === sectionId;
    button.classList.toggle('active', active);
    if (active) {
      syncSectionIndicator(button);
    }
  });
  closeMobileSidebar();
}

function initChrome() {
  if (!appShell) {
    return;
  }

  const storedCollapsed = window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === '1';
  if (!isMobileLayout()) {
    setSidebarCollapsed(storedCollapsed);
  }

  if (sidebarToggle) {
    sidebarToggle.addEventListener('click', () => {
      if (isMobileLayout()) {
        setSidebarOpen(true);
      } else {
        setSidebarCollapsed(!appShell.classList.contains('sidebar-collapsed'));
      }
    });
  }

  if (mobileNavButton) {
    mobileNavButton.addEventListener('click', () => {
      setSidebarOpen(true);
    });
  }

  if (sidebarOverlay) {
    sidebarOverlay.addEventListener('click', () => {
      setSidebarOpen(false);
    });
  }

  window.addEventListener('resize', () => {
    if (isMobileLayout()) {
      appShell.classList.remove('sidebar-collapsed');
    } else {
      setSidebarOpen(false);
      setSidebarCollapsed(window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === '1');
    }
  });
}

navButtons.forEach((button) => {
  button.addEventListener('click', () => showSection(button.dataset.section));
});

function handleUnauthorized() {
  window.location.reload();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });

  if (response.status === 401 || response.status === 403) {
    handleUnauthorized();
    throw new Error(tr('request_failed'));
  }

  if (!response.ok) {
    const data = await response.json().catch(() => ({ detail: tr('request_failed') }));
    throw new Error(data.detail || tr('request_failed'));
  }

  const contentType = response.headers.get('content-type') || '';
  return contentType.includes('application/json') ? response.json() : response;
}

function formToObject(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  Object.keys(data).forEach((key) => {
    if (data[key] === '') {
      data[key] = null;
    }
  });
  return data;
}

function setOptions(select, items, emptyLabel, selectedValue = null) {
  const options = [`<option value="">${emptyLabel}</option>`];
  items.forEach((item) => {
    options.push(`<option value="${item.id}">${item.name}</option>`);
  });
  select.innerHTML = options.join('');
  if (selectedValue !== null && selectedValue !== undefined) {
    select.value = String(selectedValue);
  }
}

function syncCredentialForm() {
  const kind = document.getElementById('credential-kind');
  if (!kind) {
    return;
  }
  const kindValue = kind.value;

  const baseUrlField = document.getElementById('base-url-field');
  const prefixField = document.getElementById('prefix-field');
  const domainField = document.getElementById('domain-field');
  const baseUrlLabel = document.getElementById('base-url-label');
  const baseUrlInput = baseUrlField ? baseUrlField.querySelector('input') : null;
  const domainInput = domainField ? domainField.querySelector('input') : null;
  const apiKeyLabel = document.getElementById('api-key-label');
  const apiKeyInput = document.getElementById('credential-api-key');

  if (apiKeyInput) {
    apiKeyInput.required = false;
  }
  if (baseUrlInput) {
    baseUrlInput.required = false;
  }

  if (kindValue === 'gptmail') {
    baseUrlField.style.display = 'grid';
    prefixField.style.display = 'grid';
    domainField.style.display = 'grid';
    if (baseUrlLabel) baseUrlLabel.textContent = 'Base URL';
    if (baseUrlInput) {
      baseUrlInput.placeholder = 'https://mail.chatgpt.org.uk';
      baseUrlInput.required = true;
    }
    if (apiKeyLabel) apiKeyLabel.textContent = 'API Key';
    if (apiKeyInput) {
      apiKeyInput.placeholder = 'API Key';
      apiKeyInput.required = true;
    }
  } else if (kindValue === 'duckmail') {
    baseUrlField.style.display = 'grid';
    prefixField.style.display = 'none';
    domainField.style.display = 'none';
    if (baseUrlLabel) baseUrlLabel.textContent = 'Base URL';
    if (baseUrlInput) baseUrlInput.placeholder = 'https://api.duckmail.sbs (可选)';
    if (apiKeyLabel) apiKeyLabel.textContent = 'Bearer Token';
    if (apiKeyInput) {
      apiKeyInput.placeholder = 'Bearer Token';
      apiKeyInput.required = true;
    }
  } else if (kindValue === 'tempmail_lol') {
    baseUrlField.style.display = 'grid';
    prefixField.style.display = 'none';
    domainField.style.display = 'none';
    if (baseUrlLabel) baseUrlLabel.textContent = 'API Base';
    if (baseUrlInput) baseUrlInput.placeholder = 'https://api.tempmail.lol/v2 (可选)';
    if (apiKeyLabel) apiKeyLabel.textContent = 'No Token Required';
    if (apiKeyInput) apiKeyInput.placeholder = '留空即可';
  } else if (kindValue === 'mail_tm') {
    baseUrlField.style.display = 'grid';
    prefixField.style.display = 'none';
    domainField.style.display = 'grid';
    if (baseUrlLabel) baseUrlLabel.textContent = 'API Base';
    if (baseUrlInput) baseUrlInput.placeholder = 'https://api.mail.tm (可选)';
    if (apiKeyLabel) apiKeyLabel.textContent = 'No Token Required';
    if (apiKeyInput) apiKeyInput.placeholder = '留空即可';
    if (domainInput) domainInput.placeholder = 'example.mail.tm (可选)';
  } else if (kindValue === 'mail_gw') {
    baseUrlField.style.display = 'grid';
    prefixField.style.display = 'none';
    domainField.style.display = 'grid';
    if (baseUrlLabel) baseUrlLabel.textContent = 'API Base';
    if (baseUrlInput) baseUrlInput.placeholder = 'https://api.mail.gw (可选)';
    if (apiKeyLabel) apiKeyLabel.textContent = 'No Token Required';
    if (apiKeyInput) apiKeyInput.placeholder = '留空即可';
    if (domainInput) domainInput.placeholder = 'example.mail.gw (可选)';
  } else if (kindValue === 'cfmail') {
    baseUrlField.style.display = 'grid';
    prefixField.style.display = 'none';
    domainField.style.display = 'none';
    if (baseUrlLabel) baseUrlLabel.textContent = 'Config Path';
    if (baseUrlInput) baseUrlInput.placeholder = 'zhuce5_cfmail_accounts.json';
    if (apiKeyLabel) apiKeyLabel.textContent = 'No Token Required';
    if (apiKeyInput) apiKeyInput.placeholder = '留空即可';
  } else if (kindValue === 'cpa') {
    baseUrlField.style.display = 'grid';
    prefixField.style.display = 'none';
    domainField.style.display = 'none';
    if (baseUrlLabel) baseUrlLabel.textContent = 'Base URL';
    if (baseUrlInput) {
      baseUrlInput.placeholder = 'https://cpa.example.com';
      baseUrlInput.required = true;
    }
    if (apiKeyLabel) apiKeyLabel.textContent = 'API Token';
    if (apiKeyInput) {
      apiKeyInput.placeholder = 'API Token';
      apiKeyInput.required = true;
    }
  } else {
    baseUrlField.style.display = 'none';
    prefixField.style.display = 'none';
    domainField.style.display = 'none';
    if (apiKeyLabel) apiKeyLabel.textContent = 'API Key';
    if (apiKeyInput) {
      apiKeyInput.placeholder = 'API Key';
      apiKeyInput.required = true;
    }
  }
}

function syncTaskForm() {
  const platformSelect = document.getElementById('platform-select');
  if (!platformSelect) {
    return;
  }
  const spec = state.platforms[platformSelect.value];
  if (!spec) {
    return;
  }

  const mailProvidersContainer = document.getElementById('mail-providers-container');
  const cpaSelectField = document.getElementById('cpa-select-field');
  const proxyModeSelect = document.getElementById('proxy-mode-select');
  const proxySelectField = document.getElementById('proxy-select-field');

  if (spec.supports_multiple_email_credentials && mailProvidersContainer) {
    mailProvidersContainer.style.display = 'grid';
    renderEmailCredentialList();
  } else if (mailProvidersContainer) {
    mailProvidersContainer.style.display = 'none';
  }

  if (spec.optional_cpa_credential && cpaSelectField) {
    cpaSelectField.style.display = 'grid';
    populateCpaSelect();
  } else if (cpaSelectField) {
    cpaSelectField.style.display = 'none';
  }

  document.getElementById('captcha-select-field').style.display = spec.requires_captcha_credential ? 'grid' : 'none';
  document.getElementById('concurrency-field').style.display = 'grid';
  if (proxyModeSelect) {
    if (!spec.supports_proxy) {
      proxyModeSelect.value = 'none';
      proxyModeSelect.disabled = true;
    } else {
      proxyModeSelect.disabled = false;
    }
  }
  if (proxySelectField) {
    proxySelectField.style.display = proxyModeSelect?.value === 'custom' && spec.supports_proxy ? 'grid' : 'none';
  }
}

const EMAIL_CREDENTIAL_KINDS = new Set(['gptmail', 'duckmail', 'tempmail_lol', 'cfmail', 'mail_tm', 'mail_gw']);
const EMAIL_KIND_LABELS = {
  gptmail: 'GPTMail',
  duckmail: 'DuckMail',
  tempmail_lol: 'TempMail.lol',
  cfmail: 'CFMail',
  mail_tm: 'Mail.tm',
  mail_gw: 'Mail.gw',
};
const selectedEmailCredentialIds = [];

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function formatDispatchScore(score) {
  return Math.round(Number(score || 0) * 100);
}

function formatDispatchWeight(weight) {
  const numeric = Number(weight || 0);
  return Number.isFinite(numeric) ? numeric.toFixed(2) : '-';
}

function formatDispatchPercent(success, total, fallback = 0) {
  const numerator = Number(success || 0);
  const denominator = Number(total || 0);
  const ratio = denominator > 0 ? numerator / denominator : fallback;
  return `${Math.round(Math.max(0, Math.min(1, ratio)) * 100)}%`;
}

function dispatchFailureCategoryLabel(category) {
  if (!category) {
    return tr('dispatch_failure_healthy');
  }
  return tr(`dispatch_failure_${category}`);
}

function dispatchStatusMeta(score, failureCategory) {
  const numericScore = Number(score || 0);
  if (failureCategory === 'mailbox_provider' || failureCategory === 'otp_delivery' || numericScore < 0.45) {
    return { key: 'risk', label: tr('dispatch_status_risk') };
  }
  if (numericScore >= 0.82) {
    return { key: 'excellent', label: tr('dispatch_status_excellent') };
  }
  if (numericScore >= 0.68) {
    return { key: 'stable', label: tr('dispatch_status_stable') };
  }
  return { key: 'watch', label: tr('dispatch_status_watch') };
}

function getDispatchRollupForCredential(credentialId) {
  const stats = getEmailCredentialStatsForCredential(credentialId);
  if (!stats.length) {
    return null;
  }
  const scoreTotal = stats.reduce((sum, item) => sum + Number(item.quality_score || 0), 0);
  const averageScore = scoreTotal / stats.length;
  const averageWeight = stats.reduce((sum, item) => sum + Number(item.dynamic_weight || 0), 0) / stats.length;
  const highestRisk = stats.find((item) => ['mailbox_provider', 'otp_delivery'].includes(item.last_failure_category))
    || stats.slice().sort((left, right) => Number(left.quality_score || 0) - Number(right.quality_score || 0))[0];
  return {
    stats,
    averageScore,
    averageWeight,
    status: dispatchStatusMeta(averageScore, highestRisk?.last_failure_category || ''),
    issueLabel: dispatchFailureCategoryLabel(highestRisk?.last_failure_category || ''),
  };
}

function createCredentialChip(text, className = 'entity-chip') {
  const chip = document.createElement('span');
  chip.className = className;
  chip.textContent = text;
  return chip;
}

function renderEmailCredentialList() {
  const container = document.getElementById('mail-providers-list');
  const selectedContainer = document.getElementById('selected-providers');
  if (!container || !selectedContainer) return;

  const emailCredentials = state.credentials.filter((item) => EMAIL_CREDENTIAL_KINDS.has(item.kind));

  container.innerHTML = emailCredentials.length
    ? emailCredentials.map((item) => {
        const isSelected = selectedEmailCredentialIds.includes(item.id);
        const meta = [EMAIL_KIND_LABELS[item.kind] || item.kind];
        if (item.kind === 'cfmail' && item.base_url) {
          meta.push(item.base_url);
        } else if (item.kind !== 'cfmail' && item.base_url) {
          meta.push(item.base_url);
        }
        if (item.domain) {
          meta.push(item.domain);
        }
        return `
          <label class="mail-provider-item ${isSelected ? 'selected' : ''}">
            <input type="checkbox" class="mail-provider-checkbox" data-credential-id="${item.id}" ${isSelected ? 'checked' : ''}>
            <span>${item.name}</span>
            <small class="mail-provider-meta">${meta.join(' | ')}</small>
          </label>
        `;
      }).join('')
    : '<span class="mail-provider-no-cred" style="color: var(--danger); font-size: 12px;">请先在凭证管理中创建邮箱凭证</span>';

  selectedContainer.innerHTML = selectedEmailCredentialIds.length > 0
    ? '<div class="selected-label">已选: </div>' + selectedEmailCredentialIds.map((id) => {
        const item = emailCredentials.find((credential) => credential.id === id);
        if (!item) return '';
        return `<span class="selected-tag">${item.name} (${kindLabels[item.kind] || item.kind})</span>`;
      }).join('')
    : '<span class="selected-label" style="color: var(--muted);">请选择至少一个邮箱凭证</span>';

  container.querySelectorAll('.mail-provider-checkbox').forEach((checkbox) => {
    checkbox.addEventListener('change', (e) => {
      const credentialId = Number(e.target.dataset.credentialId);
      const item = e.target.closest('.mail-provider-item');
      if (e.target.checked) {
        if (!selectedEmailCredentialIds.includes(credentialId)) {
          selectedEmailCredentialIds.push(credentialId);
        }
        item.classList.add('selected');
      } else {
        const idx = selectedEmailCredentialIds.indexOf(credentialId);
        if (idx >= 0) selectedEmailCredentialIds.splice(idx, 1);
        item.classList.remove('selected');
      }
      renderEmailCredentialList();
    });
  });
}

function populateCpaSelect() {
  const cpaSelect = document.getElementById('cpa-select');
  if (!cpaSelect) return;

  const cpaCredentials = state.credentials.filter((c) => c.kind === 'cpa');
  const currentValue = cpaSelect.value;

  if (cpaCredentials.length > 0) {
    cpaSelect.innerHTML = '<option value="">不上传</option>' +
      cpaCredentials.map((c) => `<option value="${c.id}">${c.name}</option>`).join('');
  } else {
    cpaSelect.innerHTML = '<option value="">不上传（请先创建CPA凭据）</option>';
  }

  if (cpaCredentials.some((c) => String(c.id) === currentValue)) {
    cpaSelect.value = currentValue;
  }
}

function renderDashboard() {
  const metrics = document.getElementById('dashboard-metrics');
  if (!metrics) {
    return;
  }

  const data = state.dashboard || {};
  metrics.innerHTML = `
    <article class="metric-card"><strong>${data.running_tasks || 0}</strong><span>${tr('dashboard_running_tasks')}</span></article>
    <article class="metric-card"><strong>${data.completed_tasks || 0}</strong><span>${tr('dashboard_completed_tasks')}</span></article>
    <article class="metric-card"><strong>${data.credential_count || 0}</strong><span>${tr('dashboard_credential_count')}</span></article>
    <article class="metric-card"><strong>${data.proxy_count || 0}</strong><span>${tr('dashboard_proxy_count')}</span></article>
  `;

  const recent = document.getElementById('dashboard-tasks');
  const tasks = data.recent_tasks || [];
  recent.innerHTML = tasks.length ? tasks.map((task) => `
    <button class="simple-row" data-task-id="${task.id}">
      <span>${task.name}</span>
      <span>${task.executed_count ?? task.results_count}/${task.quantity} | ${statusLabel(task.status)}</span>
    </button>
  `).join('') : `<p class="empty">${tr('empty_tasks')}</p>`;

  recent.querySelectorAll('[data-task-id]').forEach((button) => {
    button.addEventListener('click', () => {
      state.selectedTaskId = Number(button.dataset.taskId);
      showSection('task-detail');
      renderTaskDetail();
    });
  });
}

function renderDefaults() {
  const captchaCredentials = state.credentials.filter((item) => item.kind === 'yescaptcha');

  setOptions(
    document.getElementById('default-yescaptcha'),
    captchaCredentials,
    tr('no_default_yescaptcha'),
    state.defaults.default_yescaptcha_credential_id,
  );
  setOptions(
    document.getElementById('default-proxy'),
    state.proxies,
    tr('no_default_proxy'),
    state.defaults.default_proxy_id,
  );
}

async function saveDefaults(partial) {
  const payload = {
    default_yescaptcha_credential_id: state.defaults.default_yescaptcha_credential_id || null,
    default_proxy_id: state.defaults.default_proxy_id || null,
    ...partial,
  };
  await api('/api/defaults', { method: 'POST', body: JSON.stringify(payload) });
}

function renderCredentialsList() {
  const list = document.getElementById('credentials-list');
  const template = document.getElementById('entity-template');
  list.innerHTML = '';

  state.credentials.forEach((item) => {
    const node = template.content.cloneNode(true);
    const supportsDefault = item.kind === 'yescaptcha';
    const supportsEmailDispatch = EMAIL_CREDENTIAL_KINDS.has(item.kind);
    const isDefault = item.kind === 'yescaptcha'
      ? state.defaults.default_yescaptcha_credential_id === item.id
      : false;
    const stats = supportsEmailDispatch ? getEmailCredentialStatsForCredential(item.id) : [];

    node.querySelector('h3').textContent = item.name;
    node.querySelector('.meta').textContent = `${EMAIL_KIND_LABELS[item.kind] || item.kind} | ${tr('created_at', { value: item.created_at })}${isDefault ? ` | ${tr('default_badge')}` : ''}`;
    const notes = node.querySelector('.notes');
    notes.textContent = '';
    if (item.notes) {
      appendTextLine(notes, item.notes);
    }
    if (supportsEmailDispatch) {
      if (!stats.length) {
        appendTextLine(notes, tr('credential_dispatch_none'));
      } else {
        const rollup = getDispatchRollupForCredential(item.id);
        const line = document.createElement('div');
        line.className = 'entity-note-line';
        line.appendChild(createCredentialChip(rollup.status.label, `status-pill status-pill--${rollup.status.key}`));
        line.appendChild(createCredentialChip(`${tr('dispatch_quality_score_label')} ${formatDispatchScore(rollup.averageScore)}`, 'metric-pill'));
        line.appendChild(createCredentialChip(`${tr('credential_dispatch_platform')}: ${rollup.stats.length}`, 'entity-chip'));
        line.appendChild(createCredentialChip(`${tr('dispatch_center_issue')}: ${rollup.issueLabel}`, 'entity-chip'));
        notes.appendChild(line);
      }
    }

    const actions = node.querySelector('.entity-actions');

    if (supportsDefault) {
      const setDefaultButton = document.createElement('button');
      setDefaultButton.type = 'button';
      setDefaultButton.textContent = isDefault ? tr('current_default') : tr('set_default');
      setDefaultButton.disabled = isDefault;
      setDefaultButton.addEventListener('click', async () => {
        await saveDefaults({ default_yescaptcha_credential_id: item.id });
        await refreshState();
      });
      actions.append(setDefaultButton);
    }

    const deleteButton = document.createElement('button');
    deleteButton.type = 'button';
    deleteButton.className = 'danger';
    deleteButton.textContent = tr('delete');
    deleteButton.addEventListener('click', async () => {
      if (!window.confirm(tr('delete_credential_confirm', { name: item.name }))) {
        return;
      }
      await api(`/api/credentials/${item.id}`, { method: 'DELETE' });
      await refreshState();
    });

    actions.append(deleteButton);
    list.appendChild(node);
  });

  if (!state.credentials.length) {
    list.innerHTML = `<p class="empty">${tr('empty_credentials')}</p>`;
  }
}

function renderDispatchCenter() {
  const metricsWrap = document.getElementById('dispatch-center-metrics');
  const list = document.getElementById('dispatch-center-list');
  if (!metricsWrap || !list) {
    return;
  }

  const emailCredentials = state.credentials.filter((item) => EMAIL_CREDENTIAL_KINDS.has(item.kind));
  const statRows = state.emailCredentialStats || [];

  if (!emailCredentials.length) {
    metricsWrap.innerHTML = '';
    list.innerHTML = `<p class="empty">${tr('dispatch_center_empty')}</p>`;
    return;
  }

  const enriched = emailCredentials
    .map((item) => ({ item, rollup: getDispatchRollupForCredential(item.id) }))
    .filter((entry) => entry.rollup);

  const healthyCount = statRows.filter((item) => dispatchStatusMeta(item.quality_score, item.last_failure_category).key !== 'risk').length;
  const riskCount = statRows.filter((item) => dispatchStatusMeta(item.quality_score, item.last_failure_category).key === 'risk').length;
  const averageScore = statRows.length
    ? statRows.reduce((sum, item) => sum + Number(item.quality_score || 0), 0) / statRows.length
    : 0;
  const topEntry = enriched.slice().sort((left, right) => right.rollup.averageScore - left.rollup.averageScore)[0];

  metricsWrap.innerHTML = [
    {
      label: tr('dispatch_metric_credentials'),
      value: emailCredentials.length,
      detail: tr('dispatch_metric_credentials_desc'),
    },
    {
      label: tr('dispatch_metric_platforms'),
      value: statRows.length,
      detail: topEntry ? tr('dispatch_metric_platforms_desc', { name: topEntry.item.name, value: formatDispatchScore(averageScore) }) : tr('dispatch_metric_platforms_desc_empty'),
    },
    {
      label: tr('dispatch_metric_healthy'),
      value: healthyCount,
      detail: tr('dispatch_metric_healthy_desc'),
    },
    {
      label: tr('dispatch_metric_risk'),
      value: riskCount,
      detail: tr('dispatch_metric_risk_desc'),
    },
  ].map((metric) => `
    <article class="dispatch-overview-card">
      <p class="eyebrow">${escapeHtml(metric.label)}</p>
      <strong>${escapeHtml(metric.value)}</strong>
      <p>${escapeHtml(metric.detail)}</p>
    </article>
  `).join('');

  if (!enriched.length) {
    list.innerHTML = `<p class="empty">${tr('dispatch_center_empty')}</p>`;
    return;
  }

  list.innerHTML = enriched
    .sort((left, right) => right.rollup.averageScore - left.rollup.averageScore)
    .map(({ item, rollup }) => {
      const platformCards = rollup.stats.map((stat) => {
        const mailboxRate = formatDispatchPercent(stat.mailbox_success_count, stat.dispatch_count);
        const otpRate = formatDispatchPercent(
          stat.otp_success_count,
          stat.mailbox_success_count || stat.dispatch_count,
          stat.mailbox_success_count || stat.dispatch_count ? 0 : Number(stat.quality_score || 0),
        );
        const finalRate = formatDispatchPercent(stat.final_success_count, stat.dispatch_count);
        const status = dispatchStatusMeta(stat.quality_score, stat.last_failure_category);
        const failureCategory = stat.last_failure_category || 'healthy';
        return `
          <section class="dispatch-platform">
            <div class="dispatch-platform__top">
              <strong>${escapeHtml(stat.platform_label || stat.platform)}</strong>
              <span>${escapeHtml(tr('dispatch_quality_score_label'))} ${escapeHtml(formatDispatchScore(stat.quality_score))} / ${escapeHtml(tr('credential_dispatch_weight'))} ${escapeHtml(formatDispatchWeight(stat.dynamic_weight))}</span>
            </div>
            <div class="dispatch-platform__rail"><span style="width:${Math.max(8, formatDispatchScore(stat.quality_score))}%;"></span></div>
            <div class="dispatch-card__summary">
              <span class="status-pill status-pill--${escapeHtml(status.key)}">${escapeHtml(status.label)}</span>
              <span class="metric-pill">${escapeHtml(tr('credential_dispatch_dispatches'))} ${escapeHtml(stat.dispatch_count ?? 0)}</span>
            </div>
            <div class="dispatch-mini-grid">
              <div><span>${escapeHtml(tr('credential_dispatch_mailbox'))}</span><strong>${escapeHtml(mailboxRate)}</strong></div>
              <div><span>${escapeHtml(tr('credential_dispatch_otp'))}</span><strong>${escapeHtml(otpRate)}</strong></div>
              <div><span>${escapeHtml(tr('credential_dispatch_final'))}</span><strong>${escapeHtml(finalRate)}</strong></div>
              <div><span>${escapeHtml(tr('credential_dispatch_failures'))}</span><strong>${escapeHtml(stat.failure_count ?? 0)}</strong></div>
            </div>
            <div class="dispatch-platform__foot">
              <span class="dispatch-category dispatch-category--${escapeHtml(failureCategory)}">${escapeHtml(dispatchFailureCategoryLabel(stat.last_failure_category))}</span>
              <span class="dispatch-platform__last">${escapeHtml(tr('dispatch_center_last_event'))}: ${escapeHtml(stat.last_outcome || '-')}</span>
            </div>
          </section>
        `;
      }).join('');

      return `
        <article class="dispatch-card">
          <div class="dispatch-card__head">
            <div>
              <p class="dispatch-card__eyebrow">${escapeHtml(EMAIL_KIND_LABELS[item.kind] || item.kind)} · ${escapeHtml(tr('dispatch_center_platform_count', { count: rollup.stats.length }))}</p>
              <h3>${escapeHtml(item.name)}</h3>
              <div class="dispatch-card__summary">
                <span class="status-pill status-pill--${escapeHtml(rollup.status.key)}">${escapeHtml(rollup.status.label)}</span>
                <span class="metric-pill">${escapeHtml(tr('dispatch_quality_score_label'))} ${escapeHtml(formatDispatchScore(rollup.averageScore))}</span>
                <span class="metric-pill">${escapeHtml(tr('credential_dispatch_weight'))} ${escapeHtml(formatDispatchWeight(rollup.averageWeight))}</span>
                <span class="entity-chip">${escapeHtml(tr('dispatch_center_issue'))}: ${escapeHtml(rollup.issueLabel)}</span>
              </div>
            </div>
            <div class="dispatch-card__actions">
              <button type="button" data-dispatch-reset="${item.id}">${tr('credential_dispatch_reset')}</button>
            </div>
          </div>
          <div class="dispatch-platform-grid">${platformCards}</div>
        </article>
      `;
    }).join('');

  list.querySelectorAll('[data-dispatch-reset]').forEach((button) => {
    button.addEventListener('click', async () => {
      const credentialId = Number(button.dataset.dispatchReset);
      const item = state.credentials.find((entry) => Number(entry.id) === credentialId);
      if (!item) {
        return;
      }
      if (!window.confirm(tr('credential_dispatch_reset_confirm', { name: item.name }))) {
        return;
      }
      await api(`/api/credentials/${credentialId}/email-stats/reset`, { method: 'POST' });
      await refreshState();
    });
  });
}

function renderProxyList() {
  const list = document.getElementById('proxy-list');
  const template = document.getElementById('entity-template');
  list.innerHTML = '';

  state.proxies.forEach((item) => {
    const node = template.content.cloneNode(true);
    const isDefault = state.defaults.default_proxy_id === item.id;

    node.querySelector('h3').textContent = item.name;
    node.querySelector('.meta').textContent = `${item.proxy_url}${isDefault ? ` | ${tr('default_badge')}` : ''} | ${formatProxyStatus(item)}`;
    node.querySelector('.notes').innerHTML = `
      <div>${item.notes || ''}</div>
      ${formatProxySnapshot(item) ? `<div>${formatProxySnapshot(item)}</div>` : ''}
      <div>${formatProbeSummary(item)}</div>
      ${item.last_probe_status === 'failed' && item.last_probe_error ? `<div>${tr('proxy_test_fail')}: ${item.last_probe_error}</div>` : ''}
      ${formatProxyFeedback(item) ? `<div>${formatProxyFeedback(item)}</div>` : ''}
    `;

    const actions = node.querySelector('.entity-actions');

    const setDefaultButton = document.createElement('button');
    setDefaultButton.type = 'button';
    setDefaultButton.textContent = isDefault ? tr('current_default') : tr('set_default');
    setDefaultButton.disabled = isDefault;
    setDefaultButton.addEventListener('click', async () => {
      await saveDefaults({ default_proxy_id: item.id });
      await refreshState();
    });

    const deleteButton = document.createElement('button');
    deleteButton.type = 'button';
    deleteButton.className = 'danger';
    deleteButton.textContent = tr('delete');
    deleteButton.addEventListener('click', async () => {
      if (!window.confirm(tr('delete_proxy_confirm', { name: item.name }))) {
        return;
      }
      await api(`/api/proxies/${item.id}`, { method: 'DELETE' });
      await refreshState();
    });

    const testButton = document.createElement('button');
    testButton.type = 'button';
    testButton.textContent = tr('test_proxy');
    testButton.addEventListener('click', async () => {
      testButton.disabled = true;
      testButton.textContent = '...';
      state.proxyFeedback[item.id] = { message: 'Testing...' };
      renderProxyList();
      try {
        await api(`/api/proxies/${item.id}/test`, { method: 'POST' });
        await refreshState();
        state.proxyFeedback[item.id] = { message: '' };
        renderProxyList();
      } catch (error) {
        await refreshState();
        state.proxyFeedback[item.id] = { message: error.message };
        renderProxyList();
      } finally {
        testButton.disabled = false;
        testButton.textContent = tr('test_proxy');
      }
    });

    actions.append(setDefaultButton, testButton, deleteButton);
    list.appendChild(node);
  });

  if (!state.proxies.length) {
    list.innerHTML = `<p class="empty">${tr('empty_proxies')}</p>`;
  }
}

function renderSubscriptionList() {
  const list = document.getElementById('subscription-list');
  const template = document.getElementById('entity-template');
  list.innerHTML = '';

  state.subscriptions.forEach((item) => {
    const node = template.content.cloneNode(true);
    node.querySelector('h3').textContent = item.name;
    node.querySelector('.meta').textContent = `${tr('subscription_nodes', { count: item.node_count })} | ${tr('subscription_last_refresh', { time: item.last_refresh || '-' })}`;
    node.querySelector('.notes').textContent = item.notes || '';

    const actions = node.querySelector('.entity-actions');

    const refreshButton = document.createElement('button');
    refreshButton.type = 'button';
    refreshButton.textContent = tr('refresh_subscription');
    refreshButton.addEventListener('click', async () => {
      refreshButton.disabled = true;
      refreshButton.textContent = '...';
      try {
        const result = await api(`/api/subscriptions/${item.id}/refresh`, { method: 'POST' });
        await refreshState();
      } catch (e) {
        alert(e.message || 'Refresh failed');
      }
      refreshButton.disabled = false;
      refreshButton.textContent = tr('refresh_subscription');
    });

    const deleteButton = document.createElement('button');
    deleteButton.type = 'button';
    deleteButton.className = 'danger';
    deleteButton.textContent = tr('delete');
    deleteButton.addEventListener('click', async () => {
      if (!window.confirm(tr('delete_subscription_confirm', { name: item.name }))) {
        return;
      }
      await api(`/api/subscriptions/${item.id}`, { method: 'DELETE' });
      await refreshState();
    });

    actions.append(refreshButton, deleteButton);
    list.appendChild(node);
  });

  if (!state.subscriptions.length) {
    list.innerHTML = `<p class="empty">${tr('empty_subscriptions')}</p>`;
  }
}

function renderProxyNodesList() {
  const list = document.getElementById('proxy-nodes-list');
  list.innerHTML = '';

  // 按订阅分组
  const grouped = {};
  state.proxyNodes.forEach((node) => {
    const subId = node.subscription_id || 'external';
    if (!grouped[subId]) {
      grouped[subId] = [];
    }
    grouped[subId].push(node);
  });

  Object.entries(grouped).forEach(([subId, nodes]) => {
    const sub = state.subscriptions.find((s) => s.id === Number(subId));
    const groupName = sub ? sub.name : '外部节点';

    const groupDiv = document.createElement('div');
    groupDiv.className = 'proxy-node-group';
    groupDiv.innerHTML = `<h4 class="proxy-node-group-title">${groupName}</h4>`;

    const nodesDiv = document.createElement('div');
    nodesDiv.className = 'proxy-node-items';

    nodes.forEach((node) => {
      const nodeDiv = document.createElement('div');
      nodeDiv.className = 'proxy-node-item';
      nodeDiv.innerHTML = `
        <div class="proxy-node-info">
          <span class="proxy-node-name">${node.name}</span>
          <span class="proxy-node-meta">${node.country_name || node.country || '-'} | ${node.protocol} | ${node.server}:${node.port} | ${formatNodeProbeSummary(node)}</span>
        </div>
        <div class="proxy-node-actions">
          <button type="button" class="proxy-node-test-btn" data-node-id="${node.id}">${tr('test_proxy')}</button>
          <button type="button" class="proxy-node-use-btn" data-node-id="${node.id}">${tr('node_use')}</button>
        </div>
      `;
      nodesDiv.appendChild(nodeDiv);
    });

    groupDiv.appendChild(nodesDiv);
    list.appendChild(groupDiv);
  });

  if (!state.proxyNodes.length) {
    list.innerHTML = `<p class="empty">${tr('empty_proxy_nodes')}</p>`;
  }

  // 绑定"使用此节点"按钮事件
  list.querySelectorAll('.proxy-node-use-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const nodeId = Number(btn.dataset.nodeId);
      const node = state.proxyNodes.find((n) => n.id === nodeId);
      if (!node) return;

      // 创建一个临时代理条目
      const proxyName = `[节点] ${node.name}`;
      const proxyUrl = `node://${node.id}`;

      // 检查是否已存在同名代理
      const existing = state.proxies.find((p) => p.name === proxyName);
      if (existing) {
        // 设置为默认
        await saveDefaults({ default_proxy_id: existing.id });
        await refreshState();
        alert(`已将 "${proxyName}" 设为默认代理`);
        return;
      }

      // 创建新代理
      const result = await api('/api/proxies', {
        method: 'POST',
        body: JSON.stringify({
          name: proxyName,
          proxy_url: proxyUrl,
          notes: `${node.protocol} | ${node.server}:${node.port} | ${node.country_name || ''}`,
          snapshot_name: node.name,
          snapshot_protocol: node.protocol,
          snapshot_server: node.server,
          snapshot_port: node.port,
          snapshot_config: JSON.stringify(node.config || {}),
          snapshot_country: node.country || '',
        }),
      });

      if (result.id) {
        await saveDefaults({ default_proxy_id: result.id });
        await refreshState();
        alert(`已创建并设置 "${proxyName}" 为默认代理`);
      }
    });
  });

  list.querySelectorAll('.proxy-node-test-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const nodeId = Number(btn.dataset.nodeId);
      btn.disabled = true;
      btn.textContent = '...';
      state.nodeFeedback[nodeId] = { summary: 'Testing...' };
      renderProxyNodesList();
      try {
        const result = await api(`/api/proxy-nodes/${nodeId}/test`, { method: 'POST' });
        await refreshState();
        state.nodeFeedback[nodeId] = { summary: tr('node_test_ok', { latency: result.latency ?? '-', ip: result.exit_ip || '-' }) };
        renderProxyNodesList();
      } catch (error) {
        await refreshState();
        state.nodeFeedback[nodeId] = { summary: `${tr('node_test_fail')}: ${error.message}` };
        renderProxyNodesList();
      } finally {
        btn.disabled = false;
        btn.textContent = tr('test_proxy');
      }
    });
  });
}

function initProxyTabs() {
  const tabs = document.querySelectorAll('.proxy-tab');
  const panels = document.querySelectorAll('.proxy-panel');

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      tabs.forEach((t) => t.classList.remove('active'));
      panels.forEach((p) => p.classList.remove('active'));

      tab.classList.add('active');
      const panelId = `proxy-panel-${tab.dataset.tab}`;
      const panel = document.getElementById(panelId);
      if (panel) {
        panel.classList.add('active');
      }
    });
  });
}

function credentialNameById(id) {
  const item = state.credentials.find((entry) => entry.id === id);
  return item ? item.name : `#${id}`;
}

function proxyNameById(id) {
  const item = state.proxies.find((entry) => entry.id === id);
  return item ? item.name : `#${id}`;
}

function templateProxyModeLabel(mode) {
  if (mode === 'default') return tr('template_proxy_default');
  if (mode === 'custom') return tr('template_proxy_custom');
  if (mode === 'rotate') return tr('template_proxy_rotate');
  return tr('template_proxy_none');
}

function renderTemplatesSidebar() {
  const wrap = document.getElementById('template-list');
  if (!wrap) {
    return;
  }

  wrap.innerHTML = state.taskTemplates.length ? state.taskTemplates.map((item) => `
    <button class="task-side-item ${state.selectedTemplateId === item.id ? 'selected' : ''}" data-id="${item.id}">
      <strong>${item.name}</strong>
      <span>${item.platform} | ${item.quantity}</span>
      <span>${item.last_queued_at ? tr('template_last_queued', { value: item.last_queued_at }) : tr('template_never_queued')}</span>
    </button>
  `).join('') : `<p class="empty">${tr('empty_templates')}</p>`;

  wrap.querySelectorAll('[data-id]').forEach((button) => {
    button.addEventListener('click', () => {
      state.selectedTemplateId = Number(button.dataset.id);
      renderTemplateDetail();
    });
  });
}

function renderTemplateDetail() {
  renderTemplatesSidebar();

  const header = document.getElementById('template-detail-header');
  const actions = document.getElementById('template-detail-actions');
  const meta = document.getElementById('template-detail-meta');
  if (!header || !actions || !meta) {
    return;
  }

  const template = state.taskTemplates.find((item) => item.id === state.selectedTemplateId) || state.taskTemplates[0] || null;
  if (!template) {
    header.innerHTML = `<h3>${tr('template_detail_empty_title')}</h3><p class="meta">${tr('template_detail_empty_desc')}</p>`;
    actions.innerHTML = '';
    meta.innerHTML = '';
    return;
  }

  state.selectedTemplateId = template.id;
  const lastQueuedText = template.last_queued_at
    ? tr('template_last_queued', { value: template.last_queued_at })
    : tr('template_never_queued');
  const emailLabels = (template.email_credential_ids || []).map((id) => credentialNameById(id)).join(', ') || tr('template_not_set');
  const captchaLabel = template.captcha_credential_id ? credentialNameById(template.captcha_credential_id) : tr('template_not_set');
  const cpaLabel = template.cpa_credential_id ? credentialNameById(template.cpa_credential_id) : tr('template_cpa_disabled');
  const proxyLabel = templateProxyModeLabel(template.proxy_mode);
  const proxyTarget = template.proxy_id ? proxyNameById(template.proxy_id) : tr('template_not_set');

  header.innerHTML = `
    <div>
      <h3>${template.name} (#${template.id})</h3>
      <p class="meta">${tr('template_header_meta', {
        platform: template.platform,
        quantity: template.quantity,
        concurrency: template.concurrency,
        queue_count: template.queue_count || 0,
      })}</p>
      <p class="meta">${lastQueuedText}</p>
    </div>
  `;

  actions.innerHTML = '';

  const enqueueButton = document.createElement('button');
  enqueueButton.type = 'button';
  enqueueButton.textContent = tr('enqueue_template');
  enqueueButton.addEventListener('click', async () => {
    enqueueButton.disabled = true;
    try {
      const result = await api(`/api/task-templates/${template.id}/enqueue`, { method: 'POST' });
      state.selectedTaskId = Number(result.task_id);
      await refreshState();
    } catch (error) {
      window.alert(error.message);
    } finally {
      enqueueButton.disabled = false;
    }
  });

  const deleteButton = document.createElement('button');
  deleteButton.type = 'button';
  deleteButton.className = 'danger';
  deleteButton.textContent = tr('delete_template');
  deleteButton.addEventListener('click', async () => {
    if (!window.confirm(tr('delete_template_confirm', { name: template.name }))) {
      return;
    }
    try {
      await api(`/api/task-templates/${template.id}`, { method: 'DELETE' });
      state.selectedTemplateId = null;
      await refreshState();
    } catch (error) {
      window.alert(error.message);
    }
  });

  actions.append(enqueueButton, deleteButton);
  meta.innerHTML = `
    <article class="entity-card">
      <div>
        <h3>${tr('template_email_credentials')}</h3>
        <p class="meta">${emailLabels}</p>
        <p class="notes">${tr('template_captcha_credential')}: ${captchaLabel}</p>
        <p class="notes">${tr('template_cpa_credential')}: ${cpaLabel}</p>
        <p class="notes">${tr('template_proxy_mode')}: ${proxyLabel}</p>
        <p class="notes">${tr('template_proxy_target')}: ${proxyTarget}</p>
      </div>
    </article>
  `;
}

function getFilteredTasks() {
  if (state.taskFilterStatus === 'all') {
    return state.tasks;
  }
  return state.tasks.filter((task) => task.status === state.taskFilterStatus);
}

function renderTasksSidebar() {
  const wrap = document.getElementById('task-list');
  const tasks = getFilteredTasks();
  wrap.innerHTML = tasks.length ? tasks.map((task) => `
    <button class="task-side-item ${state.selectedTaskId === task.id ? 'selected' : ''}" data-id="${task.id}">
      <strong>${task.name}</strong>
      <span>${task.executed_count ?? task.results_count}/${task.quantity}</span>
      <span>${statusLabel(task.status)}</span>
    </button>
  `).join('') : `<p class="empty">${tr('empty_filtered_tasks')}</p>`;

  wrap.querySelectorAll('[data-id]').forEach((button) => {
    button.addEventListener('click', () => {
      state.selectedTaskId = Number(button.dataset.id);
      renderTaskDetail();
    });
  });
}

function renderTaskDetail() {
  renderTasksSidebar();

  const header = document.getElementById('task-detail-header');
  const actions = document.getElementById('task-detail-actions');
  const consoleBox = document.getElementById('task-console');
  const tasks = getFilteredTasks();
  const task = tasks.find((item) => item.id === state.selectedTaskId) || tasks[0] || null;

  if (!task) {
    header.innerHTML = `<h3>${tr('task_detail_empty_title')}</h3><p class="meta">${tr('task_detail_empty_desc')}</p>`;
    actions.innerHTML = '';
    consoleBox.textContent = tr('console_wait');
    return;
  }

  state.selectedTaskId = task.id;
  header.innerHTML = `
    <div>
      <h3>${task.name} (#${task.id})</h3>
      <p class="meta">${tr('task_header_meta', {
        platform: task.platform,
        quantity: task.quantity,
        executed: task.executed_count ?? task.results_count,
        completed: task.results_count,
        status: statusLabel(task.status),
      })}</p>
    </div>
  `;

  actions.innerHTML = '';

  const stopButton = document.createElement('button');
  stopButton.type = 'button';
  stopButton.textContent = tr('stop_task');
  stopButton.disabled = !['queued', 'running', 'stopping'].includes(task.status);
  stopButton.addEventListener('click', async () => {
    try {
      await api(`/api/tasks/${task.id}/stop`, { method: 'POST' });
      await refreshState();
    } catch (error) {
      window.alert(error.message);
    }
  });

  const downloadButton = document.createElement('button');
  downloadButton.type = 'button';
  downloadButton.textContent = tr('download_zip');
  downloadButton.disabled = ['queued', 'running', 'stopping'].includes(task.status);
  downloadButton.addEventListener('click', () => {
    window.open(`/api/tasks/${task.id}/download`, '_blank');
  });

  const deleteButton = document.createElement('button');
  deleteButton.type = 'button';
  deleteButton.className = 'danger';
  deleteButton.textContent = tr('delete_task');
  deleteButton.disabled = ['queued', 'running', 'stopping'].includes(task.status);
  deleteButton.addEventListener('click', async () => {
    if (!window.confirm(tr('delete_task_confirm', { id: task.id }))) {
      return;
    }
    try {
      await api(`/api/tasks/${task.id}`, { method: 'DELETE' });
      state.selectedTaskId = null;
      await refreshState();
    } catch (error) {
      window.alert(error.message);
    }
  });

  actions.append(stopButton, downloadButton, deleteButton);
  consoleBox.textContent = task.console_tail || tr('console_empty');
  requestAnimationFrame(() => {
    consoleBox.scrollTop = consoleBox.scrollHeight;
  });
}

function renderApiKeys() {
  const wrap = document.getElementById('api-key-list');
  wrap.innerHTML = state.apiKeys.length ? state.apiKeys.map((item) => `
    <article class="entity-card">
      <div>
        <h3>${item.name}</h3>
        <p class="meta">${tr('api_key_meta', { prefix: item.key_prefix, created_at: item.created_at })}</p>
        <p class="notes">${item.last_used_at ? tr('last_used_at', { value: item.last_used_at }) : tr('unused')}</p>
      </div>
      <div class="entity-actions">
        <button type="button" class="danger" data-id="${item.id}">${tr('delete')}</button>
      </div>
    </article>
  `).join('') : `<p class="empty">${tr('empty_api_keys')}</p>`;

  wrap.querySelectorAll('[data-id]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!window.confirm(tr('delete_api_key_confirm'))) {
        return;
      }
      await api(`/api/api-keys/${button.dataset.id}`, { method: 'DELETE' });
      await refreshState();
    });
  });
}

function populateSelectors() {
  const captchaCredentials = state.credentials.filter((item) => item.kind === 'yescaptcha');
  const captchaSelect = document.getElementById('captcha-select');
  const proxySelect = document.getElementById('proxy-select');

  const selectedCaptchaId = captchaSelect ? captchaSelect.value : '';
  const selectedProxyId = proxySelect ? proxySelect.value : '';

  const nextCaptchaId = captchaCredentials.some((item) => String(item.id) === selectedCaptchaId) ? selectedCaptchaId : '';
  const nextProxyId = state.proxies.some((item) => String(item.id) === selectedProxyId) ? selectedProxyId : '';

  setOptions(captchaSelect, captchaCredentials, tr('use_default_yescaptcha'), nextCaptchaId);
  setOptions(proxySelect, state.proxies, tr('choose_proxy'), nextProxyId);
}

async function refreshState() {
  const payload = await api('/api/state');
  state.credentials = payload.credentials;
  state.emailCredentialStats = payload.email_credential_stats || [];
  state.proxies = payload.proxies;
  state.taskTemplates = payload.task_templates || [];
  state.tasks = payload.tasks;
  state.apiKeys = payload.api_keys;
  state.defaults = payload.defaults;
  state.dashboard = payload.dashboard;
  state.platforms = payload.platforms;

  // 加载订阅和代理节点
  try {
    const subsResult = await api('/api/subscriptions');
    state.subscriptions = subsResult.subscriptions || [];

    const nodesResult = await api('/api/proxy-nodes');
    state.proxyNodes = nodesResult.nodes || [];
  } catch (e) {
    console.error('Failed to load subscriptions/nodes:', e);
    state.subscriptions = [];
    state.proxyNodes = [];
  }

  if (!state.tasks.some((task) => task.id === state.selectedTaskId)) {
    state.selectedTaskId = state.tasks[0]?.id || null;
  }
  if (!state.taskTemplates.some((item) => item.id === state.selectedTemplateId)) {
    state.selectedTemplateId = state.taskTemplates[0]?.id || null;
  }

  for (let idx = selectedEmailCredentialIds.length - 1; idx >= 0; idx -= 1) {
    if (!state.credentials.some((item) => item.id === selectedEmailCredentialIds[idx] && EMAIL_CREDENTIAL_KINDS.has(item.kind))) {
      selectedEmailCredentialIds.splice(idx, 1);
    }
  }

  populateSelectors();
  renderDefaults();
  renderDashboard();
  renderCredentialsList();
  renderDispatchCenter();
  renderProxyList();
  renderSubscriptionList();
  renderProxyNodesList();
  renderApiKeys();
  renderTemplateDetail();
  renderTaskDetail();
  syncTaskForm();
}

const credentialKind = document.getElementById('credential-kind');
if (credentialKind) {
  credentialKind.addEventListener('change', syncCredentialForm);
}

const platformSelect = document.getElementById('platform-select');
if (platformSelect) {
  platformSelect.addEventListener('change', syncTaskForm);
}

const proxyModeSelect = document.getElementById('proxy-mode-select');
if (proxyModeSelect) {
  proxyModeSelect.addEventListener('change', syncTaskForm);
}

const taskFilterStatus = document.getElementById('task-filter-status');
if (taskFilterStatus) {
  taskFilterStatus.addEventListener('change', (event) => {
    state.taskFilterStatus = event.currentTarget.value;
    renderTaskDetail();
  });
}

const logoutButton = document.getElementById('logout-btn');
if (logoutButton) {
  logoutButton.addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' });
    window.location.reload();
  });
}

const defaultsForm = document.getElementById('defaults-form');
if (defaultsForm) {
  defaultsForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = formToObject(event.currentTarget);
    ['default_yescaptcha_credential_id', 'default_proxy_id'].forEach((key) => {
      payload[key] = payload[key] ? Number(payload[key]) : null;
    });
    await api('/api/defaults', { method: 'POST', body: JSON.stringify(payload) });
    await refreshState();
  });
}

const credentialForm = document.getElementById('credential-form');
if (credentialForm) {
  credentialForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    await api('/api/credentials', { method: 'POST', body: JSON.stringify(formToObject(event.currentTarget)) });
    event.currentTarget.reset();
    syncCredentialForm();
    await refreshState();
  });
}

const proxyForm = document.getElementById('proxy-form');
if (proxyForm) {
  proxyForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    await api('/api/proxies', { method: 'POST', body: JSON.stringify(formToObject(event.currentTarget)) });
    event.currentTarget.reset();
    await refreshState();
  });
}

const subscriptionForm = document.getElementById('subscription-form');
if (subscriptionForm) {
  subscriptionForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const formData = formToObject(event.currentTarget);
    const submitBtn = event.currentTarget.querySelector('button[type="submit"]');
    const originalText = submitBtn.textContent;

    try {
      submitBtn.disabled = true;
      submitBtn.textContent = '...';

      const result = await api('/api/subscriptions', {
        method: 'POST',
        body: JSON.stringify(formData),
      });

      event.currentTarget.reset();
      await refreshState();
      alert(`订阅添加成功，解析出 ${result.node_count} 个节点`);
    } catch (e) {
      alert(e.message || '添加订阅失败');
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = originalText;
    }
  });
}

// 初始化代理选项卡
initProxyTabs();

const taskForm = document.getElementById('task-form');
if (taskForm) {
  taskForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = formToObject(event.currentTarget);
    const spec = state.platforms[payload.platform];
    payload.quantity = Number(payload.quantity);
    payload.concurrency = Number(payload.concurrency || 3);
    payload.captcha_credential_id = payload.captcha_credential_id ? Number(payload.captcha_credential_id) : null;
    payload.proxy_id = payload.proxy_id ? Number(payload.proxy_id) : null;
    payload.cpa_credential_id = payload.cpa_credential_id ? Number(payload.cpa_credential_id) : null;
    if (spec && !spec.supports_proxy) {
      payload.proxy_mode = 'none';
      payload.proxy_id = null;
    }
    if (spec?.supports_multiple_email_credentials && selectedEmailCredentialIds.length < 1) {
      window.alert('请至少选择一个邮箱凭证');
      return;
    }
    payload.email_credential_ids = selectedEmailCredentialIds.slice();

    const result = await api('/api/task-templates', { method: 'POST', body: JSON.stringify(payload) });
    await refreshState();

    selectedEmailCredentialIds.length = 0;
    renderEmailCredentialList();

    if (window.confirm(tr('created_template_confirm', { id: result.id }))) {
      state.selectedTemplateId = Number(result.id);
      showSection('template-center');
      renderTemplateDetail();
    }
  });
}

const apiKeyForm = document.getElementById('api-key-form');
if (apiKeyForm) {
  apiKeyForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const result = await api('/api/api-keys', { method: 'POST', body: JSON.stringify(formToObject(event.currentTarget)) });
    document.getElementById('api-key-created').innerHTML = `
      <div class="flash-key">
        <strong>${tr('save_now')}</strong>
        <code>${result.api_key}</code>
      </div>
    `;
    event.currentTarget.reset();
    await refreshState();
  });
}

if (appShell) {
  document.title = tr('site_title');
  initChrome();
  syncCredentialForm();
  showSection('dashboard');
  refreshState().then(() => {
    syncTaskForm();
  });
  setInterval(refreshState, 3000);
}
