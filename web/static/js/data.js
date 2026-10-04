(function () {
    'use strict';

    var clamps = document.querySelectorAll('.cell-content .clamp');
    Array.prototype.forEach.call(clamps, function (el) {
        if (el.scrollHeight <= el.clientHeight + 1) {
            return; // 未溢出三行，无需展开
        }
        var toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'clamp-toggle';
        toggle.textContent = '展开全文';
        toggle.addEventListener('click', function () {
            var expanded = el.classList.toggle('clamp-open');
            toggle.textContent = expanded ? '收起' : '展开全文';
        });
        el.parentNode.appendChild(toggle);
    });
})();
