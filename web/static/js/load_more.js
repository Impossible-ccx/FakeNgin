/* 数据页滚动追加加载：点击“加载更多”后按 offset 拉取并追加行。 */
(function () {
    var button = document.getElementById("load-more-btn");
    if (!button) return;

    var tableBody = document.querySelector("#all-messages tbody");
    var status = document.getElementById("load-more-status");
    var emptyRow = tableBody ? tableBody.querySelector("tr td.empty") : null;

    function formatProbability(value) {
        if (value === null || value === undefined || value === "") {
            return '<span class="muted">未检测</span>';
        }
        return value + "%";
    }

    function escapeHtml(text) {
        var div = document.createElement("div");
        div.textContent = text === null || text === undefined ? "" : String(text);
        return div.innerHTML;
    }

    function appendRow(row) {
        var tr = document.createElement("tr");
        tr.innerHTML =
            '<td class="cell-content"><div class="clamp">' + escapeHtml(row.content) + "</div></td>" +
            '<td><span class="nature nature-unknown">' + escapeHtml(row.nature) + "</span></td>" +
            "<td>" + formatProbability(row.fake_probability) + "</td>" +
            "<td>" + escapeHtml(row.source) + "</td>" +
            '<td class="cell-time">' + escapeHtml(row.publish_time || "—") + "</td>" +
            '<td class="ops-col"><a class="btn btn-sm" href="' + row.detail_url + '">详情</a></td>';
        tableBody.appendChild(tr);
    }

    button.addEventListener("click", function () {
        var offset = parseInt(button.getAttribute("data-offset"), 10) || 0;
        button.disabled = true;
        status.textContent = "加载中…";

        fetch("/data/more?offset=" + offset)
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (emptyRow) {
                    emptyRow.parentNode.parentNode.removeChild(emptyRow.parentNode);
                }
                data.rows.forEach(appendRow);
                var loaded = offset + data.rows.length;
                button.setAttribute("data-offset", String(loaded));
                status.textContent = "已加载 " + loaded + " / …";
                if (!data.has_more) {
                    button.disabled = true;
                    status.textContent = "已全部加载";
                } else {
                    button.disabled = false;
                }
            })
            .catch(function () {
                status.textContent = "加载失败，请重试";
                button.disabled = false;
            });
    });
})();
