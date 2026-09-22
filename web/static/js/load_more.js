/* 数据页滚动追加加载：点击“加载更多”后按 offset 拉取并追加行。 */
(function () {
    var button = document.getElementById("load-more-btn");
    if (!button) return;

    var tableBody = document.querySelector("#all-messages tbody");
    var status = document.getElementById("load-more-status");
    var emptyRow = tableBody ? tableBody.querySelector("tr td.empty") : null;

    // 与服务端 _macros.html 的风险评分口径保持一致：
    // 最新检测任务结果 > 历史/人工概率 > 未检测
    var RUN_LABELS = { pending: "排队中", running: "检测中", succeeded: "已完成",
                       failed: "失败", interrupted: "已中断" };

    function formatRisk(row) {
        if (row.latest_run) {
            if (row.latest_run.status === "succeeded" &&
                row.latest_run.probability !== null && row.latest_run.probability !== undefined) {
                var text = row.latest_run.probability + "%";
                if (row.run_stale) {
                    text += "（正文已修改·过期）";
                }
                return text;
            }
            return RUN_LABELS[row.latest_run.status] || row.latest_run.status;
        }
        if (row.fake_probability !== null && row.fake_probability !== undefined && row.fake_probability !== "") {
            var origin = row.legacy_probability ? "历史导入·来源未知" : "人工录入";
            return row.fake_probability + "%（" + origin + "）";
        }
        return null;
    }

    function escapeHtml(text) {
        var div = document.createElement("div");
        div.textContent = text === null || text === undefined ? "" : String(text);
        return div.innerHTML;
    }

    function appendRow(row) {
        var risk = formatRisk(row);
        var riskHtml = risk === null ? '<span class="muted">未检测</span>' : escapeHtml(risk);
        var tr = document.createElement("tr");
        tr.innerHTML =
            '<td class="cell-content"><div class="clamp">' + escapeHtml(row.content) + "</div></td>" +
            '<td><span class="nature nature-unknown">' + escapeHtml(row.nature) + "</span></td>" +
            "<td>" + riskHtml + "</td>" +
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
