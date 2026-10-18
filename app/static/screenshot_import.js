/* Shared screenshot workspace and version-checked clipboard actions. */
(() => {
  'use strict';
  const csrfToken = () => {
    const cookie = document.cookie.split('; ').find(row => row.startsWith('recruiting_csrf='));
    return cookie ? decodeURIComponent(cookie.slice(cookie.indexOf('=') + 1)) : '';
  };
  const showError = (form, message) => {
    const node = form.querySelector('[data-form-error]') || document.querySelector('[data-form-error]');
    if (node) { node.hidden = false; node.textContent = message; node.focus(); }
  };
  async function responseError(response) {
    if ((response.headers.get('content-type') || '').includes('application/json')) {
      const data = await response.json();
      return typeof data.error === 'string' ? data.error : typeof data.detail === 'string' ? data.detail : '提交未成功，请核对输入或刷新后重试。';
    }
    const html = new DOMParser().parseFromString(await response.text(), 'text/html');
    return html.querySelector('.screenshot-error')?.textContent.trim() || '提交未成功，请核对页面版本、登录状态和材料后重试。';
  }

  const upload = document.querySelector('[data-screenshot-upload]');
  if (upload) {
    const input = upload.querySelector('input[name="images"]');
    const list = upload.querySelector('[data-upload-list]');
    const summary = upload.querySelector('[data-upload-summary]');
    const status = upload.querySelector('[data-upload-status]');
    const dropzone = upload.querySelector('[data-dropzone]');
    let files = [];
    const bytes = () => files.reduce((total, entry) => total + entry.file.size, 0);
    const syncInput = () => {
      // FormData is built from this ordered array; input sync also supports native submission.
      if (typeof DataTransfer === 'function') {
        const transfer = new DataTransfer();
        files.forEach(entry => transfer.items.add(entry.file));
        input.files = transfer.files;
      }
    };
    const renderFiles = () => {
      list.replaceChildren();
      files.forEach((entry, index) => {
        const card = document.createElement('div'); card.className = 'screenshot-upload-card';
        const image = document.createElement('img'); image.src = entry.url; image.alt = `待上传截图 ${index + 1}`;
        const caption = document.createElement('div');
        const title = document.createElement('strong'); title.textContent = `${index + 1}. ${entry.file.name}`;
        const size = document.createElement('small'); size.textContent = `${(entry.file.size / 1048576).toFixed(2)} MiB`;
        caption.append(title, size);
        const actions = document.createElement('div'); actions.className = 'screenshot-image-actions';
        [['上移', -1], ['下移', 1], ['删除', 0]].forEach(([label, direction]) => {
          const button = document.createElement('button'); button.type = 'button'; button.className = 'button button-secondary';
          button.textContent = label; button.setAttribute('aria-label', `${label}第 ${index + 1} 张截图`);
          button.disabled = (direction === -1 && index === 0) || (direction === 1 && index === files.length - 1);
          button.addEventListener('click', () => {
            if (direction === 0) { URL.revokeObjectURL(entry.url); files.splice(index, 1); }
            else [files[index], files[index + direction]] = [files[index + direction], files[index]];
            syncInput(); renderFiles();
          }); actions.append(button);
        });
        card.append(image, caption, actions); list.append(card);
      });
      summary.textContent = files.length ? `已选择 ${files.length}/5 张 · ${(bytes() / 1048576).toFixed(2)} MiB，按上方顺序识别` : '尚未选择图片';
    };
    const addFiles = candidates => {
      const additions = Array.from(candidates).filter(file => !files.some(entry => entry.file.name === file.name && entry.file.size === file.size && entry.file.lastModified === file.lastModified));
      if (files.length + additions.length > 5) { showError(upload, '每批最多 5 张图片，请先删除不需要的截图。'); syncInput(); return; }
      if (additions.some(file => !/\.(png|jpe?g|webp)$/i.test(file.name) || file.size > 10 * 1048576)) {
        showError(upload, '请选择 PNG、JPEG 或静态 WebP；单张不能超过 10 MiB。'); syncInput(); return;
      }
      if (bytes() + additions.reduce((total, file) => total + file.size, 0) > 25 * 1048576) {
        showError(upload, '整批图片不能超过 25 MiB，请拆批上传。'); syncInput(); return;
      }
      additions.forEach(file => files.push({ file, url: URL.createObjectURL(file) }));
      upload.querySelector('[data-form-error]').hidden = true;
      syncInput(); renderFiles();
    };
    input.addEventListener('change', () => addFiles(input.files));
    ['dragenter', 'dragover'].forEach(event => dropzone.addEventListener(event, e => { e.preventDefault(); dropzone.classList.add('is-dragging'); }));
    ['dragleave', 'drop'].forEach(event => dropzone.addEventListener(event, e => { e.preventDefault(); dropzone.classList.remove('is-dragging'); }));
    dropzone.addEventListener('drop', e => addFiles(e.dataTransfer.files));
    upload.addEventListener('submit', async event => {
      event.preventDefault();
      if (!files.length) { showError(upload, '请先选择至少一张招聘截图。'); return; }
      const button = upload.querySelector('[type="submit"]');
      button.disabled = true; status.textContent = '正在上传与检查图片，请保持页面打开…';
      const data = new FormData(upload); data.delete('images');
      files.forEach(entry => data.append('images', entry.file, entry.file.name));
      try {
        const response = await fetch(upload.action, { method: 'POST', body: data, headers: { 'X-CSRF-Token': csrfToken() }, credentials: 'same-origin' });
        if (!response.ok) throw new Error(await responseError(response));
        if (!response.redirected) throw new Error('图片未返回任务入口，请保留当前材料并重试。');
        window.location.assign(response.url);
      } catch (error) {
        showError(upload, error.message || '网络连接失败，图片和文字已保留，请重试。');
        status.textContent = '尚未成功提交，已选图片和正文仍保留在本页。'; button.disabled = false;
      }
    });
  }

  const review = document.querySelector('[data-screenshot-review]');
  let dirty = false;
  let submitting = false;
  if (review) {
    const guided = review.hasAttribute?.('data-guided-review') === true;
    let fieldLabels = {};
    try { fieldLabels = JSON.parse(review.dataset.fieldLabels || '{}'); } catch (_) { /* Labels are optional presentation data. */ }
    const issueLabel = path => {
      const parts = path.split('.');
      if (parts[0] === 'fields' && parts[1]) return fieldLabels[parts[1]] || '公告字段';
      if (parts[0] === 'roles') return `岗位 ${Number(parts[1]) + 1} · ${fieldLabels[parts[2]] || '岗位条件'}`;
      if (parts[0] === 'common_requirements') return '公共条件';
      return ({ source_url: '来源原文链接', material_complete: '材料完整性', edited_text: '完整原文', acknowledge: '最终确认' })[path] || '公告内容';
    };
    const issueTarget = (path, code = '') => {
      if (code.startsWith('audience_confirmation')) {
        const proof = document.getElementById('audience-confirmation');
        if (proof) return proof;
      }
      if (code.startsWith('scope_confirmation')) {
        const proof = document.getElementById('scope-confirmation');
        if (proof) return proof;
      }
      let id = path.startsWith('fields.') ? `field-${path.slice(7)}`
        : path.startsWith('roles.') ? `role-${path.slice(6).replaceAll('.', '-')}`
        : path.startsWith('common_requirements.') ? `common-${path.slice(20)}`
        : path === 'common_requirements' ? 'common-requirements'
        : path === 'acknowledge' ? 'guided-confirm'
        : path.replaceAll('_', '-');
      return document.getElementById(id) || document.getElementById('guided-advanced');
    };
    const focusIssue = (path, code) => {
      const target = issueTarget(path, code);
      if (!target) return;
      target.hidden = false;
      let parent = target.parentElement;
      while (parent) { if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement; }
      if (target.tagName === 'DETAILS') target.open = true;
      if (!target.matches('input,textarea,select,button,a')) target.setAttribute('tabindex', '-1');
      target.focus(); target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    };
    function renderIssues(issues, focus = false) {
      const list = review.querySelector('[data-review-issues]');
      if (!list) return;
      review.querySelectorAll('[data-guided-invalid]').forEach(node => {
        node.removeAttribute('aria-invalid'); node.removeAttribute('aria-describedby'); node.removeAttribute('data-guided-invalid');
      });
      list.replaceChildren();
      const safeIssues = Array.isArray(issues) ? issues : [];
      safeIssues.forEach(issue => {
        if (String(issue.code || '').startsWith('audience_confirmation') || String(issue.code || '').startsWith('scope_confirmation')) {
          const proof = issueTarget(String(issue.path || 'fields'), String(issue.code || ''));
          if (proof) { proof.hidden = false; if (proof.tagName === 'DETAILS') proof.open = true; }
        }
      });
      safeIssues.forEach((issue, index) => {
        const li = document.createElement('li'); li.id = `review-issue-${index}`;
        li.className = `guided-issue guided-issue-${['error', 'warning', 'info'].includes(issue.severity) ? issue.severity : 'info'}`;
        const anchor = document.createElement('a'); const target = issueTarget(String(issue.path || 'fields'), String(issue.code || ''));
        anchor.href = `#${target?.id || 'guided-advanced'}`; anchor.setAttribute('data-issue-path', issue.path);
        anchor.textContent = `${({ error: '阻断', warning: '提醒', info: '说明' })[issue.severity] || '说明'} · ${issueLabel(String(issue.path || 'fields'))}：${issue.message || '请核对当前内容'}`;
        anchor.addEventListener('click', event => { event.preventDefault(); focusIssue(String(issue.path || 'fields'), String(issue.code || '')); });
        const action = document.createElement('p'); action.textContent = issue.action || '请对照原文核对';
        const code = document.createElement('small'); code.textContent = `${issue.path || 'fields'} · ${issue.code || 'review_required'}`;
        li.append(anchor, action, code); list.append(li);
        if (target && issue.severity === 'error') {
          target.setAttribute('aria-invalid', 'true'); target.setAttribute('data-guided-invalid', 'true');
          const ids = (target.getAttribute('aria-describedby') || '').split(' ').filter(Boolean);
          target.setAttribute('aria-describedby', [...ids, li.id].join(' '));
        }
      });
      const count = safeIssues.filter(issue => issue.severity === 'error').length;
      document.querySelectorAll('[data-issue-count], [data-sticky-issue-count]').forEach(node => { node.textContent = String(count); });
      review.querySelector('[data-issues-empty]').hidden = safeIssues.length > 0;
      if (focus && safeIssues.length) document.getElementById('guided-issues-title')?.focus();
    }
    function updateVersions(versions) {
      if (!versions || typeof versions !== 'object') return;
      ['expected_row_version', 'expected_job_row_version', 'expected_job_version'].forEach(name => {
        const input = review.elements.namedItem(name);
        if (input && Object.prototype.hasOwnProperty.call(versions, name)) input.value = versions[name] ?? '';
      });
    }
    function markUnsaved() {
      dirty = true;
      if (!guided) return;
      const state = review.querySelector('[data-guided-save-state]');
      if (state) state.textContent = '有尚未保存的修改。摘要显示当前输入；内部预览和问题基于已保存版本。';
      const actionState = review.querySelector('[data-guided-action-state]');
      if (actionState) actionState.textContent = '当前输入尚未保存；最终点击会先保存，再进行服务端核验';
      review.querySelectorAll('[data-summary-field]').forEach(node => {
        const key = node.dataset.summaryField; const input = review.elements.namedItem(`field_${key}`);
        if (input) node.textContent = input.value.trim() || (key === 'deadline'
          ? '当前未填日期；保存后由服务端核对原文是否存在年份或日期疑点' : '待补充');
      });
      const roles = Array.from(review.querySelectorAll('[name="selected_roles"]')).filter(input => input.checked);
      if (roles.length) {
        ['job_title', 'target_audience', 'location_detail'].forEach(key => {
          const values = roles.map(input => review.elements.namedItem(`role_${input.value}_${key}`)?.value.trim()
            || (key !== 'job_title' ? review.elements.namedItem(`field_${key}`)?.value.trim() : '')).filter(Boolean);
          const summary = review.querySelector(`[data-summary-field="${key}"]`);
          if (summary) summary.textContent = [...new Set(values)].join('、') || '所选岗位信息待补充';
        });
      }
      const source = review.querySelector('[data-summary-source]');
      if (source) source.textContent = `${review.elements.namedItem('source_name')?.value || '来源名称待补充'} · ${review.elements.namedItem('source_url')?.value || '来源链接待补充'}`;
    }
    const feedbackKey = `screenshot-feedback:${review.dataset.reviewUrl}`;
    try {
      const saved = sessionStorage.getItem(feedbackKey);
      if (saved) { const node = document.querySelector('[data-form-feedback]'); node.textContent = saved; node.hidden = false; sessionStorage.removeItem(feedbackKey); }
      const checks = JSON.parse(review.dataset.verification || '{}');
      Object.entries(checks).forEach(([name, checked]) => { const input = review.elements.namedItem(name); if (input && input.type === 'checkbox') input.checked = checked === true; });
      const selected = JSON.parse(review.dataset.selectedRoles || '[]');
      if (selected.length) review.querySelectorAll('[name="selected_roles"]').forEach(input => { input.checked = selected.includes(Number(input.value)); });
    } catch (_) { /* Invalid prior browser state never overwrites saved business data. */ }
    review.addEventListener('input', markUnsaved);
    review.addEventListener('change', markUnsaved);
    if (guided) {
      try { renderIssues(JSON.parse(review.dataset.reviewIssuesJson || '[]')); } catch (_) { /* Server-rendered issue list remains available. */ }
      review.querySelector('[data-reuse-source]')?.addEventListener('click', () => {
        const source = review.elements.namedItem('source_url'), official = review.elements.namedItem('field_official_url');
        if (source && official) { official.value = source.value; markUnsaved(); }
      });
      review.querySelectorAll('[data-show-issues]').forEach(button => button.addEventListener('click', () => document.getElementById('guided-issues-title')?.focus()));
      review.querySelector('[data-check-readiness]')?.addEventListener('click', async () => {
        try {
          const response = await fetch(review.dataset.readinessUrl, { credentials: 'same-origin', cache: 'no-store' });
          if (!response.ok) throw new Error(await responseError(response));
          const payload = await response.json(); renderIssues(payload.issues, true);
          // A read-only check cannot authorize replacing optimistic form versions.
          if (dirty) showError(review, '以上为已保存版本的问题。当前输入尚未保存，请保存草稿或点击最终确认以检查修改后的内容。');
        } catch (error) { showError(review, error.message || '暂时无法检查，请保留输入后重试。'); }
      });
    }
    review.addEventListener('submit', async event => {
      event.preventDefault();
      if (submitting) return;
      const button = event.submitter;
      const action = button?.getAttribute('formaction') || review.action;
      const roles = review.querySelectorAll('[name="selected_roles"]');
      if (roles.length && !Array.from(roles).some(input => input.checked) && !action.endsWith('/retry')) {
        showError(review, '请至少保留一个岗位。不同单位的公告请分别处理。'); return;
      }
      const data = new FormData(review);
      if (button?.name) data.set(button.name, button.value);
      const buttons = review.querySelectorAll('button[type="submit"]');
      submitting = true; buttons.forEach(node => { node.disabled = true; });
      try {
        const response = await fetch(action, { method: 'POST', body: data, headers: { 'X-CSRF-Token': csrfToken() }, credentials: 'same-origin' });
        if (guided && (response.headers.get('content-type') || '').includes('application/json')) {
          const payload = await response.json();
          // Only our own committed draft can advance the versions of retained edits.
          if (response.ok || payload.draft_saved === true) updateVersions(payload.versions);
          renderIssues(payload.issues, !response.ok);
          if (!response.ok) {
            const feedback = document.querySelector('[data-form-feedback]');
            if (feedback) { feedback.hidden = false; feedback.textContent = payload.draft_saved ? '草稿已保存，尚不能成稿；当前输入已保留。' : '尚未保存成功，当前输入已保留。'; }
            const saveState = review.querySelector('[data-guided-save-state]');
            const actionState = review.querySelector('[data-guided-action-state]');
            if (payload.draft_saved === true) {
              if (saveState) saveState.textContent = `本次提交已保存为版本 ${payload.versions?.content_revision ?? '待核对'}；红标仍需处理。当前页面保留输入，内部预览尚未刷新。`;
              if (actionState) actionState.textContent = '本次草稿已保存，处理红标后再次确认';
            } else {
              if (saveState) saveState.textContent = '本次提交未保存；当前输入已保留，请刷新比较其他页面的修改。';
              if (actionState) actionState.textContent = '本次提交未保存，请先比较最新内容';
            }
            throw new Error(payload.error || '请处理上述问题后再次确认。');
          }
          if (action.endsWith('/save')) {
            dirty = false;
            const feedback = document.querySelector('[data-form-feedback]');
            if (feedback) { feedback.hidden = false; feedback.textContent = payload.feedback || '草稿已保存。'; }
            const state = review.querySelector('[data-guided-save-state]');
            if (state) state.textContent = `草稿已保存为版本 ${payload.versions?.content_revision || ''}。摘要显示当前输入；内部预览刷新后更新。`;
            const actionState = review.querySelector('[data-guided-action-state]');
            if (actionState) actionState.textContent = '草稿已保存；最终确认仍会核验最新版本';
            const error = document.querySelector('[data-form-error]'); if (error) error.hidden = true;
            submitting = false; buttons.forEach(node => { node.disabled = false; }); return;
          }
          dirty = false; window.location.assign(payload.review_url || review.dataset.reviewUrl); return;
        }
        if (!response.ok) throw new Error(await responseError(response));
        if (!response.redirected) {
          const documentResult = new DOMParser().parseFromString(await response.text(), 'text/html');
          const message = documentResult.querySelector('[data-form-feedback]')?.textContent.trim();
          if (message) { try { sessionStorage.setItem(feedbackKey, message); } catch (_) { /* Storage is optional. */ } }
        }
        dirty = false;
        window.location.assign(response.redirected ? response.url : review.dataset.reviewUrl);
      } catch (error) {
        showError(review, error.message || '保存失败，当前输入仍保留。请检查连接后重试。');
        submitting = false; buttons.forEach(node => { node.disabled = false; });
      }
    });
  }

  const progress = document.querySelector('[data-screenshot-progress]');
  if (progress) {
    const active = new Set(['queued', 'recognizing', 'extracting']);
    const labels = { uploaded: '材料已保存', queued: '等待识别', recognizing: '正在识别', extracting: '正在整理字段', ready: '识别完成，请核对', needs_attention: '有内容需要补充或重试' };
    const started = Date.now();
    const initiallyActive = active.has(progress.dataset.initialStatus);
    let stopped = !initiallyActive;
    async function poll() {
      if (stopped) return;
      try {
        const response = await fetch(progress.dataset.statusUrl, { credentials: 'same-origin', cache: 'no-store' });
        if (!response.ok) throw new Error('暂时无法更新识别状态，稍后自动重试。');
        const state = await response.json();
        progress.querySelector('[data-progress-label]').textContent = labels[state.status] || '任务状态待核对';
        progress.querySelector('[data-piece-count]').textContent = `${state.completed}/${state.total} 片已识别`;
        progress.querySelector('[data-progress-error]').textContent = state.error || (dirty ? '你有尚未保存的修改，识别结果不会替换当前输入。' : '');
        if (!active.has(state.status)) {
          stopped = true;
          if (!dirty && !submitting) window.location.reload();
          else if (dirty) progress.querySelector('[data-progress-error]').textContent += ' 识别已结束，请先保存修改，再刷新查看完整结果。';
          return;
        }
      } catch (error) { progress.querySelector('[data-progress-error]').textContent = error.message; }
      if (!stopped) window.setTimeout(poll, Date.now() - started > 60000 ? 5000 : 2000);
    }
    if (!stopped) window.setTimeout(poll, 2000);
    window.addEventListener('pagehide', () => { stopped = true; });
  }

  document.querySelectorAll('[data-preview-tab]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('[data-preview-tab]').forEach(tab => {
      const selected = tab === button; tab.setAttribute('aria-selected', String(selected));
      document.getElementById(tab.dataset.previewTab).hidden = !selected;
    });
  }));
  const viewer = document.getElementById('screenshot-image-viewer');
  document.querySelectorAll('[data-image-zoom]').forEach(anchor => anchor.addEventListener('click', event => {
    if (viewer && typeof viewer.showModal === 'function') { event.preventDefault(); viewer.querySelector('img').src = anchor.href; viewer.showModal(); }
  }));
  document.querySelector('[data-close-image]')?.addEventListener('click', () => viewer.close());
  viewer?.addEventListener('click', event => { if (event.target === viewer) viewer.close(); });
  document.querySelectorAll('[data-evidence-asset]').forEach(anchor => anchor.addEventListener('click', () => {
    const image = document.getElementById(`asset-${anchor.dataset.evidenceAsset}`);
    image?.classList.add('is-highlighted'); window.setTimeout(() => image?.classList.remove('is-highlighted'), 4000);
    const piece = document.getElementById(`piece-${anchor.dataset.evidencePiece}`);
    if (piece) { piece.open = true; if (piece.parentElement.tagName === 'DETAILS') piece.parentElement.open = true; }
  }));

  async function writePlainText(text) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Plain clipboard API unavailable');
      await navigator.clipboard.writeText(text); return;
    } catch (_) {
      const area = document.createElement('textarea'); area.value = text;
      area.style.position = 'fixed'; area.style.opacity = '0'; document.body.append(area); area.select();
      let copied = false;
      try { copied = document.execCommand('copy'); } finally { area.remove(); }
      if (!copied) throw new Error('剪贴板不可用，复制未成功，请检查浏览器权限。');
    }
  }
  document.querySelectorAll('[data-copy-endpoint]').forEach(container => {
    const status = container.querySelector('[data-copy-status]') || document.getElementById('copy-status');
    container.querySelectorAll('[data-copy-key]').forEach(button => button.addEventListener('click', async () => {
      const key = button.dataset.copyKey;
      button.disabled = true; status.textContent = '正在核对当前状态和版本…';
      try {
        const payloadPromise = (async () => {
          const response = await fetch(container.dataset.copyEndpoint, { credentials: 'same-origin', cache: 'no-store' });
          if (!response.ok) throw new Error(await responseError(response));
          const data = await response.json();
          if (typeof data[key] !== 'string' || (key === 'html' && typeof data.plain_text !== 'string')) throw new Error('复制载荷无效，请刷新后重试。');
          return data;
        })();
        // Start the rich write during the click's user activation. ClipboardItem
        // awaits verified server data; rejection never falls back to stale DOM.
        let richWrite = null;
        if (key === 'html' && window.ClipboardItem && navigator.clipboard?.write) {
          const htmlBlob = payloadPromise.then(data => new Blob([data.html], { type: 'text/html' }));
          const plainBlob = payloadPromise.then(data => new Blob([data.plain_text], { type: 'text/plain' }));
          htmlBlob.catch(() => {}); plainBlob.catch(() => {});
          try {
            richWrite = navigator.clipboard.write([new ClipboardItem({ 'text/html': htmlBlob, 'text/plain': plainBlob })]);
            richWrite.catch(() => {});
          } catch (_) { /* Promise ClipboardItems are unavailable in some browsers. */ }
        }
        const payload = await payloadPromise;
        if (key === 'html') {
          let rich = false;
          if (richWrite) { try { await richWrite; rich = true; } catch (_) { /* Use freshly verified plain text. */ } }
          if (!rich) await writePlainText(payload.plain_text);
          status.textContent = rich ? '已复制公众号富文本正文，请粘贴后检查排版。尚未发送或发布。' : '已复制纯文本正文（富文本不可用），请粘贴后排版。尚未发送或发布。';
        } else { await writePlainText(payload[key]); status.textContent = '已复制，请自行粘贴。尚未发送或发布。'; }
      } catch (error) { status.textContent = error.message || '复制失败，请刷新并重新核对。'; }
      finally { button.disabled = false; }
    }));
  });
})();
