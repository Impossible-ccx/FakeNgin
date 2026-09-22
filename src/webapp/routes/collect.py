"""在线采集管理页（仅 admin）：查看来源、触发一次性采集、查看运行记录。

- 界面只提交来源 ID，不提供任意 URL 入口；
- 采集为一次性任务（无定时调度），是否把新消息加入检测队列
  由管理员显式勾选，默认关闭；
- POST 受 CSRF 保护，权限由服务端校验。
"""

import time

from flask import flash, redirect, render_template, request, url_for

from .. import auth, collect, collect_fetch, collect_sources, db
from . import main


@main.route("/collect")
@auth.role_required("admin")
def collect_page():
    sources = collect_sources.list_sources()
    for source in sources:
        wait = collect_fetch.next_allowed_time(source["id"]) - time.monotonic()
        source["next_wait_seconds"] = max(0, int(wait) + 1)
    return render_template(
        "collect.html",
        sources=sources,
        runs=collect.recent_runs(limit=10),
        format_labels={"rss": "RSS 2.0", "atom": "Atom", "json": "JSON"},
        now=db.now_string(),
    )


@main.route("/collect/run", methods=["POST"])
@auth.role_required("admin")
def collect_run():
    source_id = request.form.get("source_id", "").strip()
    enqueue_detection = request.form.get("enqueue_detection") == "1"
    try:
        collect_sources.get_source(source_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.collect_page"))

    run = collect.run_collection(
        source_id, enqueue_detection=enqueue_detection)

    if run["status"] == "failed":
        flash("采集失败：{}".format(run["error"]), "error")
    elif run["not_modified"]:
        flash("来源内容未更新（304），没有新条目", "success")
    else:
        flash(
            "采集完成：获取 {fetched} 条，新增 {new} 条，重复 {dup} 条，"
            "拒绝 {rej} 条，导入消息 {imported} 条（请求 {req} 次）".format(
                fetched=run["items_fetched"], new=run["items_new"],
                dup=run["items_duplicate"], rej=run["items_rejected"],
                imported=run["messages_imported"], req=run["requests"]),
            "success")
        if enqueue_detection and run["messages_imported"]:
            flash("新导入的消息已加入检测队列", "success")
    return redirect(url_for("main.collect_page"))
