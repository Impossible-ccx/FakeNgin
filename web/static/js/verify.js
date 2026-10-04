(function () {
    'use strict';
    document.querySelectorAll('[data-verify-cancel]').forEach(function (button) {
        button.hidden = false;
        button.addEventListener('click', function () {
            var details = button.closest('details');
            if (!details) return;
            var form = button.closest('form');
            if (form) form.reset();
            details.open = false;
            details.querySelector('summary').focus();
        });
    });
    document.querySelectorAll('.verify-record-editor, .verify-record-delete').forEach(function (details) {
        details.addEventListener('toggle', function () {
            if (!details.open) return;
            var siblingDetails = details.parentElement.querySelectorAll(':scope > details');
            siblingDetails.forEach(function (other) { if (other !== details) other.open = false; });
            var input = details.querySelector('textarea, button[type="submit"]');
            if (input) input.focus({preventScroll: true});
        });
    });
    document.querySelectorAll('[data-verify-form]').forEach(function (form) {
        form.addEventListener('submit', function (event) {
            if (form.dataset.submitting === 'true') { event.preventDefault(); return; }
            if (!form.reportValidity()) { event.preventDefault(); return; }
            form.dataset.submitting = 'true';
            form.setAttribute('aria-busy', 'true');
            form.querySelectorAll('button[type="submit"]').forEach(function (button) { button.disabled = true; });
        });
    });
    window.addEventListener('pageshow', function () {
        document.querySelectorAll('[data-verify-form]').forEach(function (form) {
            delete form.dataset.submitting;
            form.removeAttribute('aria-busy');
            form.querySelectorAll('button[type="submit"]').forEach(function (button) { button.disabled = false; });
        });
    });
})();
