(function () {
    'use strict';
    if (!window.fetch || !window.FormData) return;
    var form = document.getElementById('data-batch-form');
    var box = document.getElementById('batch-progress-box');
    if (!form || !box) return;
    var rows = Array.from(form.querySelectorAll('input[name="items"]'));
    var models = Array.from(form.querySelectorAll('input[name="models"]'));
    var modelList = document.getElementById('batch-model-list');
    var refreshForm = document.getElementById('batch-model-refresh');
    var refreshButton = document.getElementById('batch-model-refresh-button');
    var modelCheckStatus = document.getElementById('batch-model-check-status');
    var submit = document.getElementById('batch-submit');
    var count = document.getElementById('batch-selected-count');
    var statusMessage = document.getElementById('batch-status-message');
    var selectPage = document.getElementById('batch-select-page');
    var clearSelection = document.getElementById('batch-clear-selection');
    var maxRows = Number(form.getAttribute('data-max-rows')) || 100;
    var selectionKey = 'fakengin.batch.selection.v1';
    var selectedRows = new Map();
    function rowKey(reference) { return JSON.stringify([reference.file, reference.row]); }
    try {
        var remembered = JSON.parse(window.sessionStorage.getItem(selectionKey) || '[]');
        if (Array.isArray(remembered)) remembered.slice(0, maxRows).forEach(function (reference) {
            if (reference && typeof reference.file === 'string' && Number.isInteger(reference.row) && typeof reference.signature === 'string') selectedRows.set(rowKey(reference), reference);
        });
    } catch (error) { /* Page selection still works when browser storage is disabled. */ }
    rows.forEach(function (input) {
        var reference = JSON.parse(input.value);
        var previous = selectedRows.get(rowKey(reference));
        input.checked = Boolean(previous && previous.signature === reference.signature);
        if (previous && previous.signature !== reference.signature) selectedRows.delete(rowKey(reference));
    });
    function rememberSelection() {
        try { window.sessionStorage.setItem(selectionKey, JSON.stringify(Array.from(selectedRows.values()))); } catch (error) { /* Optional persistence. */ }
    }
    function setRow(input, checked) {
        var reference = JSON.parse(input.value);
        input.checked = checked;
        if (checked) selectedRows.set(rowKey(reference), reference);
        else selectedRows.delete(rowKey(reference));
        rememberSelection();
    }
    var creating = false;
    var checkingModels = false;
    var modelSelectionEdited = form.getAttribute('data-model-preferences') === 'true';
    var modeSelectionEdited = modelSelectionEdited;
    var jobActive = false;
    var polling = false;
    var pollTimer;
    var statusUrl = box.getAttribute('data-status-url');
    var cancelUrl = box.getAttribute('data-cancel-url');
    var states = {queued: '等待开始', running: '正在检测', cancelling: '正在停止', completed: '检测完成', cancelled: '已取消', interrupted: '任务已中断'};
    var itemStates = {pending: '等待检测', running: '正在检测', completed: '已完成', failed: '未获得完整结果', cancelled: '已取消', interrupted: '未完成'};
    var memberStates = {running: '正在分析', ok: '已完成', unavailable: '服务未连接', error: '检测失败', abstained: '未能评分'};

    function node(tag, className, text) {
        var element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== undefined && text !== null) element.textContent = text;
        return element;
    }
    function localUrl(value) {
        if (typeof value !== 'string' || value.charAt(0) !== '/' || value.slice(0, 2) === '//') throw new Error('返回的页面地址异常。');
        return value;
    }
    async function jsonRequest(url, options) {
        var response = await fetch(url, Object.assign({credentials: 'same-origin', headers: {Accept: 'application/json'}}, options));
        var data = await response.json().catch(function () { throw new Error('服务返回内容异常，请稍后重试。'); });
        if (!response.ok) throw new Error(data.error || data.message || '操作未完成，请稍后重试。');
        return data;
    }
    function chosenModels() { return models.filter(function (input) { return input.checked; }); }
    function mode() {
        var input = form.querySelector('input[name="mode"]:checked');
        return input ? input.value : 'vote';
    }
    function available(input) { return input.getAttribute('data-available') === 'true'; }
    function updateSelection(changed) {
        if (mode() === 'single') {
            var chosen = chosenModels();
            var keep = changed && changed.checked ? changed : chosen.find(available) || chosen[0];
            models.forEach(function (input) { if (input !== keep) input.checked = false; });
        }
        models.forEach(function (input) { input.closest('.risk-model').classList.toggle('risk-selected', input.checked); });
        var selected = selectedRows.size;
        if (count) count.textContent = selected;
        var chosen = chosenModels();
        var ready = chosen.filter(available).length;
        var connectedCount = models.filter(available).length;
        var needsProbe = models.some(function (input) { return input.getAttribute('data-needs-probe') === 'true'; });
        var availableCount = document.getElementById('batch-model-available-count');
        if (availableCount) availableCount.textContent = connectedCount;
        var voteMode = form.querySelector('input[name="mode"][value="vote"]');
        if (voteMode) voteMode.disabled = connectedCount < 2;
        if (refreshButton) refreshButton.disabled = checkingModels || creating;
        var modelHelp = document.getElementById('batch-selection-help');
        if (modelHelp) {
            if (checkingModels) modelHelp.textContent = '正在检查模型连接…';
            else if (needsProbe) modelHelp.textContent = '尚未检查模型连接。点击“检查模型连接”后选择参与模型。';
            else if (!connectedCount) modelHelp.textContent = '未找到已连接的分析模型，启动模型服务后可重新检查连接。';
            else modelHelp.textContent = ready < chosen.length ? '已选 ' + chosen.length + ' 个模型，' + ready + ' 个可用；有效票不足时取有效分数均值。' : '每条消息使用相同的 ' + chosen.length + ' 个模型配置，逐条顺序检测。';
        }
        var validModels = mode() === 'single' ? chosen.length === 1 : chosen.length >= 2 && chosen.length <= 3;
        submit.disabled = creating || checkingModels || jobActive || !selected || selected > maxRows || !validModels || !ready;
        if (statusMessage) {
            if (jobActive) statusMessage.textContent = '当前批量任务正在执行，可在下方查看进度。';
            else if (!rows.length && !selected) statusMessage.textContent = '先导入消息，再选择样本开始检测。';
            else if (!selected) statusMessage.textContent = '勾选待检测的样本，或选择本页全部消息。';
            else if (selected > maxRows) statusMessage.textContent = '每批最多检测 ' + maxRows + ' 条，请减少选择。';
            else if (needsProbe) statusMessage.textContent = '先检查模型连接，再选择参与模型。';
            else if (!validModels) statusMessage.textContent = mode() === 'single' ? '请选择一个模型。' : '投票需要选择 2–3 个不同的模型。';
            else if (!ready) statusMessage.textContent = '所选模型尚未就绪，请选择至少一个可用模型。';
            else statusMessage.textContent = '已选 ' + selected + ' 条消息（跨页保留），' + ready + ' 个模型可用。每条完整结果自动保存到历史记录。';
        }
        if (selectPage) selectPage.disabled = !rows.length || creating;
        if (clearSelection) clearSelection.disabled = !selected || creating;
    }
    rows.forEach(function (input) { input.addEventListener('change', function () { setRow(input, input.checked); updateSelection(); }); });
    function bindModelChoices() {
        models.forEach(function (input) { input.addEventListener('change', function () { modelSelectionEdited = true; updateSelection(input); }); });
    }
    bindModelChoices();
    Array.from(form.querySelectorAll('input[name="mode"]')).forEach(function (input) { input.addEventListener('change', function () { modeSelectionEdited = true; updateSelection(); }); });
    if (selectPage) selectPage.addEventListener('click', function () { rows.forEach(function (input) { setRow(input, true); }); updateSelection(); });
    if (clearSelection) clearSelection.addEventListener('click', function () { selectedRows.clear(); rows.forEach(function (input) { input.checked = false; }); rememberSelection(); updateSelection(); });

    function renderModels(items, previousIds, previousMode) {
        if (!modelList || !Array.isArray(items)) throw new Error('模型连接状态异常，请稍后重试。');
        var connected = items.filter(function (item) { return item && typeof item.id === 'string' && item.available === true; });
        var selectedMode = connected.length < 2 ? 'single' : modeSelectionEdited ? previousMode : 'vote';
        var selectedIds = previousIds.filter(function (id) { return connected.some(function (item) { return item.id === id; }); });
        if (selectedMode === 'single') selectedIds = selectedIds.length ? selectedIds.slice(0, 1) : connected.slice(0, 1).map(function (item) { return item.id; });
        else if (!modelSelectionEdited) selectedIds = connected.map(function (item) { return item.id; });
        modelList.replaceChildren();
        connected.forEach(function (item, index) {
            var card = node('div', 'risk-model batch-model-card');
            var label = node('label', 'risk-model-choice');
            var input = document.createElement('input');
            input.type = 'checkbox';
            input.name = 'models';
            input.value = item.id;
            input.setAttribute('data-available', 'true');
            input.checked = selectedIds.indexOf(item.id) !== -1;
            label.appendChild(input);
            var title = typeof item.display_name === 'string' ? item.display_name : '分析模型 ' + (index + 1);
            var mark = node('span', 'model-mark', title.split(' ').pop());
            mark.setAttribute('aria-hidden', 'true');
            label.appendChild(mark);
            var copy = node('span', 'risk-model-copy');
            var modelTitle = node('span', 'risk-model-title');
            modelTitle.appendChild(node('strong', '', title));
            modelTitle.appendChild(node('span', 'risk-availability risk-availability-ready', '已连接'));
            copy.appendChild(modelTitle);
            copy.appendChild(node('span', 'risk-model-description', item.description || '独立分析消息，给出风险评分和判断理由。'));
            label.appendChild(copy);
            card.appendChild(label);
            modelList.appendChild(card);
        });
        if (!connected.length) modelList.appendChild(node('p', 'risk-empty-models', '暂无已连接的分析模型，启动模型服务后可重新检查连接。'));
        models = Array.from(form.querySelectorAll('input[name="models"]'));
        Array.from(form.querySelectorAll('input[name="mode"]')).forEach(function (input) { input.checked = input.value === selectedMode; });
        bindModelChoices();
        updateSelection();
        return connected.length;
    }
    if (refreshForm && modelList) refreshForm.addEventListener('submit', async function (event) {
        event.preventDefault();
        if (checkingModels || creating) return;
        var previousIds = chosenModels().map(function (input) { return input.value; });
        var previousMode = mode();
        checkingModels = true;
        if (modelCheckStatus) modelCheckStatus.textContent = '正在检查模型连接…';
        updateSelection();
        try {
            var data = await jsonRequest(refreshForm.action, {method: 'POST'});
            var connectedCount = renderModels(data.models, previousIds, previousMode);
            if (modelCheckStatus) modelCheckStatus.textContent = connectedCount ? '已连接 ' + connectedCount + ' 个分析模型。' : '未找到已连接的模型，启动模型服务后可再次检查。';
        } catch (error) {
            if (modelCheckStatus) modelCheckStatus.textContent = error instanceof TypeError ? '连接检查失败，请稍后重试。' : error.message;
        } finally {
            checkingModels = false;
            updateSelection();
        }
    });

    function reportLink(url, text) {
        var link = node('a', 'batch-report-link', text || '查看报告 ↗');
        link.href = localUrl(url);
        return link;
    }
    function renderJob(job) {
        if (!job || !Array.isArray(job.items) || !states[job.status]) throw new Error('任务状态异常，请刷新查看。');
        jobActive = ['queued', 'running', 'cancelling'].indexOf(job.status) !== -1;
        box.dataset.jobId = job.id;
        box.dataset.jobStatus = job.status;
        var keepCancelFocus = document.activeElement && document.activeElement.id === 'batch-cancel-button';
        var panel = node('section', 'batch-job-panel');
        panel.setAttribute('aria-labelledby', 'batch-job-title');
        var heading = node('div', 'batch-job-heading');
        var title = node('div');
        title.appendChild(node('p', 'risk-eyebrow', 'BATCH / 批量任务'));
        var jobTitle = node('h2', '', states[job.status]);
        jobTitle.id = 'batch-job-title';
        title.appendChild(jobTitle);
        heading.appendChild(title);
        var actions = node('div', 'batch-job-actions');
        actions.appendChild(reportLink('/data?job=' + encodeURIComponent(job.id), '任务固定链接 ↗'));
        if (jobActive) {
            var stop = node('button', 'dash-button dash-button-small', job.status === 'cancelling' ? '正在停止…' : '取消后续检测');
            stop.type = 'button';
            stop.id = 'batch-cancel-button';
            stop.disabled = job.status === 'cancelling';
            actions.appendChild(stop);
        }
        heading.appendChild(actions);
        panel.appendChild(heading);
        var facts = node('dl', 'batch-job-facts');
        [['选定消息', job.total], ['已处理', job.completed_count || 0], ['已保存报告', job.saved_count || 0], ['未获有效结果', job.failed_count || 0]].forEach(function (fact) {
            var pair = node('div');
            pair.appendChild(node('dt', '', fact[0]));
            pair.appendChild(node('dd', '', fact[1]));
            facts.appendChild(pair);
        });
        panel.appendChild(facts);
        var track = node('progress', 'batch-job-track');
        track.max = job.total || 1;
        track.value = job.completed_count || 0;
        track.setAttribute('aria-label', '已处理消息数量');
        panel.appendChild(track);
        if (job.error) {
            var error = node('p', 'batch-job-error', job.error);
            error.setAttribute('role', 'alert');
            panel.appendChild(error);
        }
        var current = job.items.find(function (item) { return item.status === 'running'; });
        if (current) {
            var live = node('div', 'batch-job-current');
            live.appendChild(node('h3', '', '正在检测第 ' + (current.index + 1) + ' / ' + job.total + ' 条消息'));
            live.appendChild(node('p', 'batch-job-message', current.message));
            var members = node('div', 'risk-live-members batch-job-models');
            (current.members || []).forEach(function (member, index) {
                var card = node('article', 'risk-live-member batch-job-model');
                card.dataset.status = member.status;
                card.appendChild(node('h4', '', member.ui_display_name || '分析模型 ' + (index + 1)));
                card.appendChild(node('span', 'risk-live-status', memberStates[member.status] || '等待分析'));
                card.appendChild(node('strong', 'risk-live-score', typeof member.score === 'number' ? member.score.toFixed(1) + ' 分' : '—'));
                card.appendChild(node('p', 'risk-live-reason', member.reason || member.error || '正在读取消息并生成判断…'));
                members.appendChild(card);
            });
            live.appendChild(members);
            panel.appendChild(live);
        }
        if (jobActive) panel.appendChild(node('p', 'risk-help', '刷新页面可继续查看。取消后，当前模型返回时停止；已完成的报告会保留。'));
        else if (job.status === 'interrupted') panel.appendChild(node('p', 'risk-help', '服务重启时任务已停止，已有报告保留；可以选择未完成的样本重新检测。'));
        var list = node('div', 'batch-job-items');
        job.items.forEach(function (item) {
            var row = node('article', 'batch-job-item');
            row.dataset.status = item.status;
            row.dataset.index = item.index;
            row.appendChild(node('span', 'batch-job-index', String(item.index + 1).padStart(2, '0')));
            var content = node('div', 'batch-job-item-content');
            content.appendChild(node('p', 'batch-job-message', item.message));
            var metadata = node('div', 'batch-job-meta');
            metadata.appendChild(node('span', '', itemStates[item.status] || item.status));
            if (item.result) metadata.appendChild(node('span', 'batch-result-badge risk-level-' + item.result.level, item.result.label));
            if (item.history_url) metadata.appendChild(reportLink(item.history_url));
            content.appendChild(metadata);
            if (item.error || item.history_error) content.appendChild(node('p', 'batch-job-error', item.error || item.history_error));
            if (item.history_error && item.result) {
                var unsaved = node('details', 'batch-unsaved-result');
                unsaved.appendChild(node('summary', '', '查看本次未保存的结果'));
                item.result.members.forEach(function (member, index) {
                    unsaved.appendChild(node('p', 'risk-help', (member.ui_display_name || '分析模型 ' + (index + 1)) + '：' + (typeof member.score === 'number' ? member.score.toFixed(1) + ' 分；' : '') + (member.reason || member.error || '未能评分')));
                });
                content.appendChild(unsaved);
            }
            row.appendChild(content);
            list.appendChild(row);
            if (item.history_url && item.result) {
                rows.forEach(function (input) {
                    if (input.dataset.file !== item.file || Number(input.dataset.row) !== item.row || input.dataset.signature !== item.signature) return;
                    var cell = input.closest('tr').querySelector('[data-row-result]');
                    if (cell) {
                        cell.replaceChildren(node('span', 'batch-result-badge risk-level-' + item.result.level, item.result.label), reportLink(item.history_url));
                    }
                });
            }
        });
        panel.appendChild(list);
        var history = node('div', 'batch-job-actions');
        history.appendChild(reportLink('/history', '打开历史记录 ↗'));
        panel.appendChild(history);
        box.replaceChildren(panel);
        if (keepCancelFocus) {
            var newStop = box.querySelector('#batch-cancel-button');
            if (newStop && !newStop.disabled) newStop.focus({preventScroll: true});
        }
        updateSelection();
    }
    function showPollError(message) {
        var existing = box.querySelector('[data-batch-connection-error]');
        if (existing) existing.remove();
        var error = node('p', 'batch-job-error', message + ' 后台任务仍可继续，请稍后刷新查看。');
        error.setAttribute('data-batch-connection-error', '');
        error.setAttribute('role', 'alert');
        box.prepend(error);
    }
    async function poll() {
        if (!statusUrl || polling) return;
        polling = true;
        try {
            var data = await jsonRequest(localUrl(statusUrl));
            renderJob(data.job);
        } catch (error) {
            showPollError(error instanceof TypeError ? '暂时无法读取任务进度。' : error.message);
        } finally {
            polling = false;
            if (jobActive) pollTimer = setTimeout(poll, 1000);
        }
    }
    form.addEventListener('submit', async function (event) {
        event.preventDefault();
        updateSelection();
        if (submit.disabled || !form.reportValidity()) return;
        creating = true;
        updateSelection();
        try {
            var references = Array.from(selectedRows.values());
            var data = await jsonRequest(form.action, {
                method: 'POST', headers: {Accept: 'application/json', 'Content-Type': 'application/json'},
                body: JSON.stringify({rows: references, model_ids: chosenModels().map(function (input) { return input.value; }), mode: mode()})
            });
            statusUrl = localUrl(data.status_url);
            cancelUrl = localUrl(data.cancel_url);
            box.dataset.statusUrl = statusUrl;
            box.dataset.cancelUrl = cancelUrl;
            if (data.job_url && window.history.replaceState) window.history.replaceState(null, '', localUrl(data.job_url));
            rows.forEach(function (input) { input.checked = false; });
            selectedRows.clear();
            rememberSelection();
            renderJob(data.job);
            clearTimeout(pollTimer);
            pollTimer = setTimeout(poll, 350);
            box.scrollIntoView({behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start'});
        } catch (error) {
            if (statusMessage) statusMessage.textContent = error instanceof TypeError ? '连接失败，请重试。' : error.message;
        } finally {
            creating = false;
            // Keep a request error visible until the user changes a selection.
            if (jobActive) updateSelection();
            else submit.disabled = false;
        }
    });
    box.addEventListener('click', async function (event) {
        var stop = event.target.closest('#batch-cancel-button');
        if (!stop || !cancelUrl) return;
        event.preventDefault();
        stop.disabled = true;
        stop.textContent = '正在停止…';
        try {
            var data = await jsonRequest(localUrl(cancelUrl), {method: 'POST'});
            renderJob(data.job);
            clearTimeout(pollTimer);
            if (jobActive) pollTimer = setTimeout(poll, 350);
        } catch (error) {
            stop.disabled = false;
            stop.textContent = '取消后续检测';
            showPollError(error.message);
        }
    });
    Array.from(document.querySelectorAll('form[data-import-form]')).forEach(function (importForm) {
        importForm.addEventListener('submit', async function (event) {
            event.preventDefault();
            if (!importForm.reportValidity()) return;
            var body = new FormData(importForm);
            var controls = Array.from(importForm.elements);
            var disabled = controls.map(function (control) { return control.disabled; });
            var status = importForm.querySelector('[data-import-status]');
            controls.forEach(function (control) { control.disabled = true; });
            if (status) status.textContent = '正在导入…';
            try {
                var data = await jsonRequest(importForm.action, {method: 'POST', body: body});
                window.location.assign(localUrl(data.data_url));
            } catch (error) {
                if (status) status.textContent = error instanceof TypeError ? '连接失败，请重试。' : error.message;
                controls.forEach(function (control, index) { control.disabled = disabled[index]; });
            }
        });
    });
    if (selectPage) selectPage.hidden = false;
    if (clearSelection) clearSelection.hidden = false;
    jobActive = ['queued', 'running', 'cancelling'].indexOf(box.dataset.jobStatus) !== -1;
    updateSelection();
    if (statusUrl) poll();
})();
