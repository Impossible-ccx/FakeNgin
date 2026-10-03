(function () {
    'use strict';
    var form = document.getElementById('api-settings-form');
    if (!form || !window.fetch) return;
    var clearForm = document.getElementById('api-settings-clear-form');
    var key = document.getElementById('deepseek-api-key');
    var model = document.getElementById('deepseek-api-model');
    var status = document.getElementById('api-settings-status');
    var busy = false;
    var controls = Array.from(form.elements);
    if (clearForm) controls = controls.concat(Array.from(clearForm.elements));
    controls.forEach(function (control) { if (control.tagName === 'BUTTON') control.disabled = false; });

    async function send(url, payload, clearing) {
        if (busy) return;
        busy = true;
        var disabled = controls.map(function (control) { return control.disabled; });
        controls.forEach(function (control) { control.disabled = true; });
        status.textContent = clearing ? '正在清除个人 API…' : '正在保存个人 API…';
        status.classList.remove('api-settings-error');
        try {
            var response = await fetch(url, {
                method: 'POST', credentials: 'same-origin', cache: 'no-store',
                headers: {'Content-Type': 'application/json', Accept: 'application/json'},
                body: JSON.stringify(payload)
            });
            // The password is kept only while entering it, never in browser storage.
            key.value = '';
            var data = await response.json().catch(function () { throw new Error('服务返回内容异常，请重试。'); });
            if (!response.ok) throw new Error(data.error || data.message || 'API 设置未能保存，请重试。');
            status.textContent = clearing ? '个人 API 已清除，正在更新页面…' : '个人 API 已配置，正在更新页面…';
            window.location.reload();
        } catch (error) {
            key.value = '';
            status.classList.add('api-settings-error');
            status.textContent = error instanceof TypeError ? '连接失败，请重新填写 Key 后重试。' : error.message;
            controls.forEach(function (control, index) { control.disabled = disabled[index]; });
            busy = false;
            key.focus();
        }
    }
    form.addEventListener('submit', function (event) {
        event.preventDefault();
        if (busy || !form.reportValidity()) return;
        send(form.action, {api_key: key.value.trim(), model_name: model.value}, false);
    });
    if (clearForm) clearForm.addEventListener('submit', function (event) {
        event.preventDefault();
        send(clearForm.action, {}, true);
    });
    window.addEventListener('pagehide', function () { key.value = ''; });
})();
