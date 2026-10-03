(function () {
    var form = document.getElementById('detect-form');
    if (!form) {
        return;
    }
    var box = document.getElementById('detect-result-box');
    var button = form.querySelector('button[type="submit"]');
    var checkUrl = form.getAttribute('data-check-url');

    form.addEventListener('submit', function (event) {
        event.preventDefault();

        var startedAt = Date.now();
        button.disabled = true;
        box.innerHTML = '<div class="detect-loading">' +
            '<div class="detect-spinner"></div>' +
            '<div>正在检测，已耗时 <span class="detect-timer">0</span> 秒</div>' +
            '<div class="detect-loading-hint">本地大模型推理通常需要 20~120 秒，请勿关闭页面</div>' +
            '</div>';

        var timer = setInterval(function () {
            var el = box.querySelector('.detect-timer');
            if (el) {
                el.textContent = Math.floor((Date.now() - startedAt) / 1000);
            }
        }, 1000);

        var body = new URLSearchParams(new FormData(form)).toString();

        fetch(checkUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
            body: body
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error('HTTP ' + response.status);
                }
                return response.text();
            })
            .then(function (html) {
                box.innerHTML = html;
            })
            .catch(function () {
                box.innerHTML = '<div class="notice notice-error">网络请求失败，请重试</div>';
            })
            .finally(function () {
                clearInterval(timer);
                button.disabled = false;
            });
    });
})();
