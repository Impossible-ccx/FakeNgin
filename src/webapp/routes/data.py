"""数据导入、样本浏览与可恢复的批量检测任务。"""

import json

from flask import current_app, jsonify, redirect, render_template, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from checkmodel.ensemble import MAX_MESSAGE_LENGTH

from .. import batches, dataset, newsdata
from ..models import list_web_models, validate_web_source
from . import main

PAGE_SIZE = 20


def _context():
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    message_table = newsdata.load_all()
    total = len(message_table)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > total_pages:
        page = total_pages

    start = (page - 1) * PAGE_SIZE
    rows = message_table.iloc[start:start + PAGE_SIZE].to_dict("records")
    references = [{"file": row["_file"], "row": int(row["_row"]), "signature": row["_signature"]}
                  for row in rows]
    latest = batches.latest_reports(references)
    for row in rows:
        row["latest_report"] = latest.get((row["_file"], int(row["_row"]), row["_signature"]))
    recent_jobs = batches.list_batches(limit=5)
    requested_job = request.args.get("job", "")
    current_job = batches.get_batch(requested_job) if requested_job else next(
        (job for job in recent_jobs if job["status"] in batches.ACTIVE_STATUSES), None,
    )
    models = list_web_models()
    available_ids = [model["id"] for model in models if model["available"]]
    selected_ids = available_ids
    mode = "vote" if len(selected_ids) > 1 else "single"
    return dict(
        rows=rows,
        page=page,
        total=total,
        total_pages=total_pages,
        models=models,
        default_selected_ids=current_job["model_ids"] if current_job else selected_ids,
        default_mode=current_job["mode"] if current_job else mode,
        active_job_id=current_job["id"] if current_job else None,
        current_job=current_job,
        recent_jobs=recent_jobs,
        imported_count=max(0, min(dataset.MAX_IMPORT_ROWS, request.args.get("imported", 0, type=int))),
        data_error="批量任务不存在" if requested_job and current_job is None else None,
        batch_limits={"max_import_rows": dataset.MAX_IMPORT_ROWS,
                      "max_import_bytes": dataset.MAX_IMPORT_BYTES,
                      "max_batch_rows": dataset.MAX_BATCH_ROWS,
                      "max_message_length": MAX_MESSAGE_LENGTH},
    )


def _wants_json():
    return request.is_json or request.accept_mimetypes.best == "application/json"


def _error(message, status=400):
    if _wants_json():
        return jsonify(error=message), status
    context = _context()
    context.update(data_error=message, import_error=message, batch_error=message)
    return render_template("data.html", **context), status


@main.route("/data")
def data():
    query = request.args.get("q", "").strip()
    if query:
        return redirect(url_for("main.search", q=query))
    return render_template("data.html", **_context())


@main.route("/data/import", methods=["POST"])
def data_import():
    # Permit multipart framing while bounding the actual file/text separately.
    request.max_content_length = dataset.MAX_IMPORT_BYTES + 65536
    request.max_form_memory_size = dataset.MAX_IMPORT_BYTES + 65536
    try:
        uploads = [file for file in request.files.getlist("file") if file.filename]
        text = request.form.get("text", "")
        source = request.form.get("source", "").strip()
        if len(uploads) > 1 or bool(uploads) == bool(text.strip()):
            raise ValueError("请选择一个 CSV 文件，或粘贴消息文本，两种方式任选其一")
        if uploads:
            file = uploads[0]
            if not file.filename.lower().endswith(".csv"):
                raise ValueError("请选择 CSV 文件")
            result = dataset.import_csv(file.stream.read(dataset.MAX_IMPORT_BYTES + 1), source=source)
        else:
            result = dataset.import_text(text, source=source)
    except RequestEntityTooLarge:
        return _error("导入内容不能超过 1 MiB", 413)
    except ValueError as exc:
        return _error(str(exc))
    except Exception:
        current_app.logger.warning("Dataset import failed")
        return _error("数据导入失败，请检查文件格式和本地存储后重试。", 500)
    result["data_url"] = url_for("main.data", imported=result["imported_count"])
    if _wants_json():
        return jsonify(result), 201
    return redirect(result["data_url"], code=303)


@main.route("/data/batches", methods=["POST"])
def batch_start():
    request.max_content_length = dataset.MAX_IMPORT_BYTES
    try:
        if request.is_json:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValueError("批量检测请求格式无效")
            rows, model_ids = payload.get("rows"), payload.get("model_ids")
            mode = payload.get("mode", "vote")
            source = payload.get("deepseek_source")
        else:
            rows = [json.loads(value) for value in (request.form.getlist("items") or request.form.getlist("rows"))]
            model_ids = request.form.getlist("models")
            mode = request.form.get("mode", "vote")
            source = request.form.get("deepseek_source")
        source = validate_web_source(source)
        job = batches.create_batch(rows, model_ids, mode, source)
    except RequestEntityTooLarge:
        return _error("批量请求内容过大", 413)
    except ValueError as exc:
        return _error("消息选择格式无效，请刷新页面" if isinstance(exc, json.JSONDecodeError) else str(exc))
    except Exception:
        current_app.logger.warning("Batch creation failed")
        return _error("批量任务创建失败，请检查本地存储后重试。", 500)
    job_url = url_for("main.data", job=job["id"])
    if _wants_json():
        return jsonify(job_id=job["id"], job=job, job_url=job_url,
                       status_url=url_for("main.batch_status", job_id=job["id"]),
                       cancel_url=url_for("main.batch_cancel", job_id=job["id"])), 202
    return redirect(job_url, code=303)


@main.route("/data/batches/<job_id>")
def batch_status(job_id):
    job = batches.get_batch(job_id)
    if job is None:
        return jsonify(error="批量任务不存在"), 404
    response = jsonify(job=job)
    response.headers["Cache-Control"] = "no-store"
    return response


@main.route("/data/batches/<job_id>/cancel", methods=["POST"])
def batch_cancel(job_id):
    job = batches.cancel_batch(job_id)
    if job is None:
        return _error("批量任务不存在", 404)
    if _wants_json():
        return jsonify(job=job)
    return redirect(url_for("main.data", job=job_id), code=303)
