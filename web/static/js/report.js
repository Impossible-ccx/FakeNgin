(function () {
    'use strict';

    function localizeTimes(root) {
        root.querySelectorAll('time[data-local-time]').forEach(function (element) {
            var date = new Date(element.getAttribute('datetime'));
            if (!Number.isNaN(date.getTime())) {
                element.textContent = date.toLocaleString('zh-CN', {
                    year: 'numeric', month: '2-digit', day: '2-digit',
                    hour: '2-digit', minute: '2-digit', hour12: false
                });
                element.title = date.toLocaleString('zh-CN');
            }
        });
    }

    async function copyText(text) {
        if (navigator.clipboard && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(text);
                return;
            } catch (_) { /* Fall back to selection when clipboard permission is denied. */ }
        }
        var input = document.createElement('textarea');
        input.value = text;
        input.setAttribute('readonly', '');
        input.style.position = 'fixed';
        input.style.opacity = '0';
        document.body.appendChild(input);
        input.select();
        try {
            if (!document.execCommand('copy')) throw new Error('请使用下载文本报告保存结果。');
        } finally { input.remove(); }
    }

    document.addEventListener('click', async function (event) {
        var button = event.target.closest('[data-copy-report]');
        if (!button || button.disabled) return;
        var status = button.parentElement.querySelector('[data-copy-status]');
        var label = button.textContent;
        button.disabled = true;
        if (status) status.textContent = '正在复制…';
        try {
            var response = await fetch(button.getAttribute('data-copy-report'), {credentials: 'same-origin'});
            if (!response.ok) throw new Error('报告读取失败，请稍后重试。');
            await copyText(await response.text());
            if (status) status.textContent = '报告已复制';
            else button.textContent = '已复制';
        } catch (error) {
            if (status) status.textContent = error.message || '复制失败，请下载文本报告。';
            else button.textContent = '复制失败，请下载';
        } finally {
            button.disabled = false;
            if (status) button.textContent = label;
        }
    });

    window.FakeNginReports = {localizeTimes: localizeTimes};
    localizeTimes(document);
})();
