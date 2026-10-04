(function () {
    'use strict';

    var form = document.getElementById('detect-form');
    if (!form || !window.fetch || !window.FormData || !window.URLSearchParams) {
        return; // Keep the normal form submission available without JavaScript support.
    }

    var box = document.getElementById('detect-result-box');
    var button = form.querySelector('button[type="submit"]');
    var message = document.getElementById('risk-message');
    var count = document.getElementById('risk-char-count');
    var selectionHelp = document.getElementById('risk-selection-help');
    var modelInputs = Array.from(form.querySelectorAll('input[name="models"]'));
    var classifierInputs = Array.from(form.querySelectorAll('input[name="classifier_model"]'));
    var riskGroup = form.querySelector('[data-model-group="risk"]');
    var classifierGroup = form.querySelector('[data-model-group="classifier"]');
    var modeInputs = Array.from(form.querySelectorAll('input[name="mode"]'));
    var example = document.getElementById('risk-example');
    var clear = document.getElementById('risk-clear');
    var busy = false;

    function currentMode() {
        var checked = form.querySelector('input[name="mode"]:checked');
        return checked ? checked.value : 'vote';
    }

    function selectedModels() {
        return modelInputs.filter(function (input) { return input.checked && !input.disabled; });
    }

    function isAvailable(input) {
        return input.getAttribute('data-available') === 'true';
    }

    function updateSelection(changedInput) {
        if (busy) return;
        var mode = currentMode();
        var classifierMode = mode === 'classifier';
        if (riskGroup) riskGroup.hidden = classifierMode;
        if (classifierGroup) classifierGroup.hidden = !classifierMode;
        if (classifierMode) {
            classifierInputs.forEach(function (input) {
                input.closest('.risk-model').classList.toggle('risk-selected', input.checked);
            });
            var hasClassifier = classifierInputs.length > 0;
            selectionHelp.textContent = hasClassifier
                ? '已选择真假分类器；输出虚假概率，与语言风险分含义不同。'
                : '暂无可用分类模型，请先完成准备步骤。';
            button.disabled = !hasClassifier || !classifierInputs.some(function (input) {
                return input.checked;
            });
            return;
        }
        if (mode === 'single') {
            var selected = selectedModels();
            var keep = changedInput && changedInput.checked ? changedInput : selected.find(isAvailable) || selected[0];
            modelInputs.forEach(function (input) {
                if (input !== keep) input.checked = false;
            });
        }
        var chosen = selectedModels().length;
        var chosenAvailable = selectedModels().filter(isAvailable).length;
        var available = modelInputs.filter(isAvailable).length;
        var valid = (mode === 'single' ? chosen === 1 : chosen >= 2 && chosen <= 3) && chosenAvailable > 0;
        modelInputs.forEach(function (input) {
            input.closest('.risk-model').classList.toggle('risk-selected', input.checked && !input.disabled);
        });
        if (!available) {
            selectionHelp.textContent = '暂无可用模型，请先完成下方准备步骤。';
        } else if (mode === 'single') {
            selectionHelp.textContent = chosen && !chosenAvailable ? '所选模型未就绪，请选择一个可用模型。' : '已选择 ' + chosen + ' 个模型；单模型分析需要选择 1 个可用模型。';
        } else if (chosen >= 2 && chosen <= 3 && !chosenAvailable) {
            selectionHelp.textContent = '所选模型均未就绪，请至少加入一个可用模型。';
        } else if (chosen >= 2 && chosen <= 3 && chosenAvailable < chosen) {
            selectionHelp.textContent = '已选 ' + chosen + ' 个模型（' + chosenAvailable + ' 个可用）。未就绪模型保留在名单中，但不计入平均分。';
        } else {
            selectionHelp.textContent = '已选择 ' + chosen + ' 个模型；需要 2–3 个。优先多数票，无多数时使用有效分数均值。';
        }
        button.disabled = !valid;
    }

    function updateMessage() {
        count.textContent = message.value.length + ' / ' + message.maxLength + ' 字符';
        message.setCustomValidity('');
        example.disabled = Boolean(message.value.trim());
        example.title = example.disabled ? '清空内容后可填入示例' : '填入一条演示用消息';
    }

    modelInputs.forEach(function (input) {
        input.addEventListener('change', function () { updateSelection(input); });
    });
    classifierInputs.forEach(function (input) {
        input.addEventListener('change', function () { updateSelection(); });
    });
    modeInputs.forEach(function (input) {
        input.addEventListener('change', function () { updateSelection(); });
    });
    message.addEventListener('input', updateMessage);
    example.hidden = false;
    clear.hidden = false;
    example.addEventListener('click', function () {
        if (message.value.trim()) {
            message.focus();
            return;
        }
        message.value = '群聊转发：明早全市停水，请大家尽快储水并转发。消息未附具体通知链接。';
        updateMessage();
        message.focus();
    });
    clear.addEventListener('click', function () {
        message.value = '';
        updateMessage();
        message.focus();
    });
    updateMessage();
    updateSelection();

    function focusResult() {
        var heading = box.querySelector('[tabindex="-1"]');
        if (heading) heading.focus({ preventScroll: true });
    }

    form.addEventListener('submit', function (event) {
        event.preventDefault();
        if (busy) return;
        if (!message.value.trim()) {
            message.setCustomValidity('请输入需要分析的消息内容。');
        }
        if (!form.reportValidity()) return;
        updateSelection();
        if (button.disabled) return;

        // Capture repeated "models" fields before disabling the controls.
        var body = new URLSearchParams(new FormData(form)).toString();
        var controls = Array.from(form.elements).filter(function (control) {
            return control.tagName !== 'FIELDSET';
        });
        var disabledBefore = controls.map(function (control) { return control.disabled; });
        var startedAt = Date.now();
        busy = true;
        controls.forEach(function (control) { control.disabled = true; });
        button.querySelector('span').textContent = '正在检测…';
        box.setAttribute('aria-busy', 'true');
        box.innerHTML = '<section class="risk-loading" role="status">' +
            '<span class="risk-spinner" aria-hidden="true"></span>' +
            '<div><h2>正在分析消息</h2>' +
            '<p>已耗时 <span class="risk-timer">0</span> 秒 · 本机模型依次运行，完成后统一展示结果。</p>' +
            '<p class="risk-help">请保持页面打开。这里显示的是本次请求的等待时间。</p></div></section>';

        var timer = setInterval(function () {
            var timerNode = box.querySelector('.risk-timer');
            if (timerNode) timerNode.textContent = Math.floor((Date.now() - startedAt) / 1000);
        }, 1000);

        fetch(form.getAttribute('data-check-url'), {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
            body: body
        })
            .then(function (response) {
                return response.text().then(function (html) {
                    // Validation failures can return a fragment with a non-2xx status.
                    var fragment = document.createElement('template');
                    fragment.innerHTML = html;
                    if (!fragment.content.querySelector('[data-risk-result]')) {
                        throw new Error('Unexpected response: ' + response.status);
                    }
                    box.replaceChildren(fragment.content.cloneNode(true));
                });
            })
            .catch(function () {
                box.innerHTML = '<section class="risk-error" role="alert" tabindex="-1">' +
                    '<h2>暂时无法获取检测结果</h2>' +
                    '<p>连接中断或请求失败。输入内容已保留，请确认服务状态后重试。</p></section>';
            })
            .finally(function () {
                clearInterval(timer);
                busy = false;
                controls.forEach(function (control, index) { control.disabled = disabledBefore[index]; });
                button.querySelector('span').textContent = '开始检测';
                box.setAttribute('aria-busy', 'false');
                updateSelection();
                focusResult();
            });
    });
})();
