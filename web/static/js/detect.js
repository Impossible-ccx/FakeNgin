(function () {
    'use strict';
    var form = document.getElementById('detect-form');
    if (!form || !window.fetch || !window.FormData || !window.URLSearchParams) return;
    var box = document.getElementById('detect-result-box');
    var button = form.querySelector('button[type="submit"]');
    var message = document.getElementById('risk-message');
    var count = document.getElementById('risk-char-count');
    var selectionHelp = document.getElementById('risk-selection-help');
    var modelInputs = Array.from(form.querySelectorAll('input[name="models"]'));
    var modeInputs = Array.from(form.querySelectorAll('input[name="mode"]'));
    var example = document.getElementById('risk-example');
    var clear = document.getElementById('risk-clear');
    var busy = false;
    var statuses = {queued: '等待分析', running: '正在分析', ok: '已完成', error: '检测失败', unavailable: '服务未连接', abstained: '未能评分'};

    function node(tag, className, text) {
        var element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== undefined) element.textContent = text;
        return element;
    }
    function currentMode() {
        var checked = form.querySelector('input[name="mode"]:checked');
        return checked ? checked.value : 'vote';
    }
    function selectedModels() {
        return modelInputs.filter(function (input) { return input.checked && !input.disabled; });
    }
    function isAvailable(input) { return input.getAttribute('data-available') === 'true'; }
    function updateSelection(changedInput) {
        if (busy) return;
        if (currentMode() === 'single') {
            var selected = selectedModels();
            var keep = changedInput && changedInput.checked ? changedInput : selected.find(isAvailable) || selected[0];
            modelInputs.forEach(function (input) { if (input !== keep) input.checked = false; });
        }
        var chosen = selectedModels().length;
        var chosenAvailable = selectedModels().filter(isAvailable).length;
        var available = modelInputs.filter(isAvailable).length;
        var valid = (currentMode() === 'single' ? chosen === 1 : chosen >= 2 && chosen <= 3) && chosenAvailable > 0;
        modelInputs.forEach(function (input) {
            input.closest('.risk-model').classList.toggle('risk-selected', input.checked && !input.disabled);
        });
        if (!available) selectionHelp.textContent = '模型服务尚未连接，连接后即可开始检测。';
        else if (currentMode() === 'single') selectionHelp.textContent = chosen && !chosenAvailable ? '请选择一个已连接的模型。' : '已选择 ' + chosen + ' 个模型，独立分析消息。';
        else if (chosen >= 2 && chosen <= 3 && !chosenAvailable) selectionHelp.textContent = '所选模型均未连接，请至少选择一个可用模型。';
        else if (chosen >= 2 && chosen <= 3 && chosenAvailable < chosen) selectionHelp.textContent = '已选 ' + chosen + ' 个模型，' + chosenAvailable + ' 个可用；仅有效分数参与平均。';
        else selectionHelp.textContent = '已选择 ' + chosen + ' 个模型；选择 2–3 个，优先多数票，分歧时取均分。';
        button.disabled = !valid;
    }
    function updateMessage() {
        count.textContent = message.value.length + ' / ' + message.maxLength + ' 字符';
        message.setCustomValidity('');
        example.disabled = Boolean(message.value.trim());
        example.title = example.disabled ? '清空内容后可填入示例' : '填入一条示例消息';
    }
    modelInputs.forEach(function (input) { input.addEventListener('change', function () { updateSelection(input); }); });
    modeInputs.forEach(function (input) { input.addEventListener('change', function () { updateSelection(); }); });
    message.addEventListener('input', updateMessage);
    example.hidden = false;
    clear.hidden = false;
    example.addEventListener('click', function () {
        if (message.value.trim()) return message.focus();
        message.value = '群聊转发：明早全市停水，请大家尽快储水并转发。消息未附具体通知链接。';
        updateMessage();
        message.focus();
    });
    clear.addEventListener('click', function () { message.value = ''; updateMessage(); message.focus(); });
    updateMessage();
    updateSelection();

    function createLivePanel(inputs) {
        var panel = node('section', 'risk-live');
        panel.setAttribute('aria-labelledby', 'risk-live-title');
        var header = node('div', 'risk-live-header');
        var heading = node('div');
        heading.appendChild(node('p', 'risk-eyebrow', 'LIVE ANALYSIS / 实时分析'));
        var title = node('h2', '', '模型正在独立分析');
        title.id = 'risk-live-title';
        heading.appendChild(title);
        header.appendChild(heading);
        var timer = node('span', 'risk-live-timer', '已耗时 0 秒');
        timer.setAttribute('aria-live', 'off');
        header.appendChild(timer);
        panel.appendChild(header);
        var completedLabel = node('p', 'risk-live-count', '0 / ' + inputs.length + ' 个模型已返回');
        completedLabel.setAttribute('role', 'status');
        panel.appendChild(completedLabel);
        var progress = node('progress', 'risk-live-track');
        progress.max = inputs.length;
        progress.value = 0;
        progress.setAttribute('aria-label', '已返回的模型数量');
        panel.appendChild(progress);
        var list = node('div', 'risk-live-members');
        var rows = new Map();
        inputs.forEach(function (input) {
            var label = input.closest('.risk-model').querySelector('.risk-model-title strong');
            var row = node('article', 'risk-live-member');
            row.dataset.status = 'queued';
            row.dataset.modelId = input.value;
            row.appendChild(node('h3', '', label ? label.textContent : input.value));
            var status = node('span', 'risk-live-status', statuses.queued);
            var score = node('strong', 'risk-live-score', '—');
            var reason = node('p', 'risk-live-reason', '轮到此模型时会开始分析。');
            row.appendChild(status);
            row.appendChild(score);
            row.appendChild(reason);
            list.appendChild(row);
            rows.set(input.value, {row: row, status: status, score: score, reason: reason});
        });
        panel.appendChild(list);
        box.replaceChildren(panel);
        var completed = new Set();
        return {
            panel: panel, timer: timer,
            update: function (member, done) {
                var item = rows.get(member.id);
                if (!item) throw new Error('返回了未知模型，请重新检测。');
                item.row.dataset.status = member.status;
                item.status.textContent = statuses[member.status] || '处理中';
                if (done) {
                    completed.add(member.id);
                    item.score.textContent = typeof member.score === 'number' ? member.score.toFixed(1) + ' 分' : '—';
                    item.reason.textContent = member.reason || member.error || '本次未返回有效分数。';
                } else item.reason.textContent = '正在读取消息并生成判断…';
                completedLabel.textContent = completed.size + ' / ' + inputs.length + ' 个模型已返回';
                progress.value = completed.size;
                if (completed.size === inputs.length) title.textContent = '模型已返回，正在整理报告';
            },
            interrupt: function () {
                title.textContent = '本次分析连接已中断';
                rows.forEach(function (item, id) {
                    if (completed.has(id)) return;
                    item.row.dataset.status = 'interrupted';
                    item.status.textContent = '未收到结果';
                    item.reason.textContent = '连接已中断，该模型的最终结果尚未返回。';
                });
            }
        };
    }
    function showReport(html) {
        var fragment = document.createElement('template');
        fragment.innerHTML = html;
        if (!fragment.content.querySelector('[data-risk-result]')) throw new Error('返回内容异常，请稍后重试。');
        box.replaceChildren(fragment.content.cloneNode(true));
        if (window.FakeNginReports) window.FakeNginReports.localizeTimes(box);
        var heading = box.querySelector('[tabindex="-1"]');
        if (heading) heading.focus({preventScroll: true});
    }
    async function readEvents(response, onEvent) {
        if (!response.ok) {
            var error = await response.json().catch(function () { return {}; });
            throw new Error(error.message || error.error || '请求未能开始，请检查输入后重试。');
        }
        function consume(line) { if (line.trim()) onEvent(JSON.parse(line)); }
        if (!response.body || !response.body.getReader || !window.TextDecoder) {
            (await response.text()).split('\n').forEach(consume);
            return;
        }
        var reader = response.body.getReader();
        var decoder = new TextDecoder('utf-8');
        var pending = '';
        try {
            while (true) {
                var part = await reader.read();
                pending += part.done ? decoder.decode() : decoder.decode(part.value, {stream: true});
                var newline;
                while ((newline = pending.indexOf('\n')) !== -1) {
                    consume(pending.slice(0, newline));
                    pending = pending.slice(newline + 1);
                }
                if (part.done) break;
            }
            consume(pending);
        } catch (error) {
            await reader.cancel().catch(function () {});
            throw error;
        } finally { reader.releaseLock(); }
    }

    form.addEventListener('submit', async function (event) {
        event.preventDefault();
        if (busy) return;
        if (!message.value.trim()) message.setCustomValidity('请输入需要分析的消息内容。');
        if (!form.reportValidity()) return;
        updateSelection();
        if (button.disabled) return;
        var selected = selectedModels();
        var body = new URLSearchParams(new FormData(form)).toString();
        var controls = Array.from(form.elements).filter(function (control) { return control.tagName !== 'FIELDSET'; });
        var disabledBefore = controls.map(function (control) { return control.disabled; });
        var startedAt = Date.now();
        var complete = false;
        busy = true;
        controls.forEach(function (control) { control.disabled = true; });
        button.querySelector('span').textContent = '分析进行中…';
        // Keep member announcements accessible rather than marking the whole region busy.
        box.setAttribute('aria-busy', 'false');
        var live = createLivePanel(selected);
        var timer = setInterval(function () {
            live.timer.textContent = '已耗时 ' + Math.floor((Date.now() - startedAt) / 1000) + ' 秒';
        }, 1000);
        try {
            var streamUrl = form.getAttribute('data-stream-url');
            var response = await fetch(streamUrl || form.getAttribute('data-check-url'), {
                method: 'POST', credentials: 'same-origin',
                headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'}, body: body
            });
            if (streamUrl) {
                await readEvents(response, function (data) {
                    if (data.type === 'member_start') live.update(data.member, false);
                    else if (data.type === 'member_complete') live.update(data.member, true);
                    else if (data.type === 'complete') { showReport(data.html); complete = true; }
                    else if (data.type === 'error') throw new Error(data.message || '检测中断，请稍后重试。');
                });
            } else {
                if (!response.ok) throw new Error('请求失败，请稍后重试。');
                showReport(await response.text());
                complete = true;
            }
            if (!complete) throw new Error('连接已中断，尚未获得完整报告。');
        } catch (error) {
            if (!complete) {
                live.interrupt();
                var alert = node('section', 'risk-error');
                alert.setAttribute('role', 'alert');
                alert.setAttribute('tabindex', '-1');
                var symbol = node('span', 'error-symbol', '!');
                symbol.setAttribute('aria-hidden', 'true');
                alert.appendChild(symbol);
                var explanation = node('div');
                explanation.appendChild(node('h2', '', '本次分析未完成'));
                var detail = error instanceof SyntaxError ? '返回内容异常，请稍后重试。' :
                    error instanceof TypeError ? '网络连接中断，请检查服务状态后重试。' : error.message || '连接中断，请稍后重试。';
                explanation.appendChild(node('p', '', detail));
                explanation.appendChild(node('p', 'risk-help', '输入与已返回的模型结果已保留，可重新开始检测。'));
                alert.appendChild(explanation);
                box.prepend(alert);
                alert.focus({preventScroll: true});
            }
        } finally {
            clearInterval(timer);
            busy = false;
            controls.forEach(function (control, index) { control.disabled = disabledBefore[index]; });
            button.querySelector('span').textContent = '开始检测';
            box.setAttribute('aria-busy', 'false');
            updateSelection();
            updateMessage();
        }
    });
})();
