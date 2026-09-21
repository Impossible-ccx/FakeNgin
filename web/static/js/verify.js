document.addEventListener("DOMContentLoaded", function () {
    function showEdit(button) {
        var viewRow = button.closest("tr");
        var editRow = viewRow.nextElementSibling;
        if (!editRow) return;
        viewRow.hidden = true;
        editRow.hidden = false;
    }

    function hideEdit(button) {
        var editRow = button.closest("tr");
        var viewRow = editRow.previousElementSibling;
        if (!viewRow) return;
        editRow.hidden = true;
        viewRow.hidden = false;
    }

    function showReview(button) {
        var viewRow = button.closest("tr");
        var reviewRow = viewRow.nextElementSibling;
        if (!reviewRow || !reviewRow.classList.contains("review-row")) return;
        viewRow.hidden = true;
        reviewRow.hidden = false;
    }

    function hideReview(button) {
        var reviewRow = button.closest("tr");
        var viewRow = reviewRow.previousElementSibling;
        if (!viewRow) return;
        reviewRow.hidden = true;
        viewRow.hidden = false;
    }

    function showDeleteConfirm(button) {
        var ops = button.closest(".ops-col");
        ops.querySelector(".js-edit").hidden = true;
        button.hidden = true;
        ops.querySelector(".del-confirm").hidden = false;
    }

    function hideDeleteConfirm(button) {
        var ops = button.closest(".ops-col");
        ops.querySelector(".del-confirm").hidden = true;
        ops.querySelector(".js-edit").hidden = false;
        ops.querySelector(".js-del").hidden = false;
    }

    document.querySelectorAll(".js-edit").forEach(function (button) {
        button.addEventListener("click", function () { showEdit(button); });
    });

    document.querySelectorAll(".js-cancel").forEach(function (button) {
        button.addEventListener("click", function () { hideEdit(button); });
    });

    document.querySelectorAll(".js-review").forEach(function (button) {
        button.addEventListener("click", function () { showReview(button); });
    });

    document.querySelectorAll(".js-review-cancel").forEach(function (button) {
        button.addEventListener("click", function () { hideReview(button); });
    });

    document.querySelectorAll(".js-del").forEach(function (button) {
        button.addEventListener("click", function () { showDeleteConfirm(button); });
    });

    document.querySelectorAll(".js-del-cancel").forEach(function (button) {
        button.addEventListener("click", function () { hideDeleteConfirm(button); });
    });
});
