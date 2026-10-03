"""语言风险等级优先等权投票，无多数时按有效分数均值分档。"""

import math
from time import perf_counter

import checkmodel

from .base import CheckError, RiskAbstention


MAX_MESSAGE_LENGTH = 6000
DEEPSEEK_SOURCES = ("cloud", "local")
_SOURCE_LABELS = {"cloud": "云端 API", "local": "本地 Ollama"}
RISK_MODELS = (
    {
        "id": "qwen2.5_7b",
        "display_name": "Qwen2.5-7B (Ollama)",
        "description": "使用 Qwen2.5-7B 评估消息的语言风险。",
    },
    {
        "id": "deepseek_r1",
        "display_name": "DeepSeek-R1 (Ollama)",
        "description": "使用 DeepSeek-R1 评估消息的语言风险。",
    },
    {
        "id": "glm4_9b",
        "display_name": "GLM-4-9B (Ollama)",
        "description": "使用 GLM-4-9B 评估消息的语言风险。",
    },
)
_MODEL_BY_ID = {model["id"]: model for model in RISK_MODELS}
_LEVEL_LABELS = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
    "uncertain": "无法判断",
}


def get_risk_models(deepseek_source=None, cloud_model=None):
    """始终列出三个正式风险模型，并标明当前是否可用。"""
    _validate_deepseek_source(deepseek_source)
    available_models = {model["id"]: model for model in checkmodel.get_models()}
    models = []
    for model in RISK_MODELS:
        metadata = {**model, "available": model["id"] in available_models}
        configured = available_models.get(model["id"], {})
        for field in ("display_name", "description"):
            if isinstance(configured.get(field), str) and configured[field].strip():
                metadata[field] = configured[field]
        if model["id"] == "deepseek_r1":
            sources = [dict(item) for item in checkmodel.get_model_sources("deepseek_r1")]
            if cloud_model is not None:
                for item in sources:
                    if item["source"] == "cloud":
                        item.update(display_name=cloud_model.display_name, description=cloud_model.description,
                                    model_name=cloud_model.model_name, available=bool(cloud_model.detect()))
            default_source = next((item["source"] for item in sources if item["available"]), "cloud")
            if cloud_model is not None and getattr(cloud_model, "credential_mode", None) == "expired":
                default_source = "cloud"
            source = deepseek_source or default_source
            selected_source = next(item for item in sources if item["source"] == source)
            metadata.update(
                sources=sources, default_source=default_source, source=source,
                source_label=selected_source["source_label"],
                display_name=selected_source["display_name"],
                description=selected_source["description"], available=selected_source["available"],
            )
        models.append(metadata)
    return models


def _validate_deepseek_source(deepseek_source):
    if deepseek_source is not None and deepseek_source not in DEEPSEEK_SOURCES:
        raise ValueError("请选择有效的 DeepSeek 来源：云端 API 或本地 Ollama")


def validate_risk_request(message, model_ids, mode="vote", deepseek_source=None):
    """在启动推理前验证请求，返回规范化消息和选定模型列表。"""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("请输入消息内容")
    message = message.strip()
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ValueError("消息内容不能超过 {} 个字符".format(MAX_MESSAGE_LENGTH))
    if mode not in ("vote", "single"):
        raise ValueError("请选择有效的检测模式")
    _validate_deepseek_source(deepseek_source)
    if not isinstance(model_ids, (list, tuple)):
        raise ValueError("请选择检测模型")

    selected = []
    for model_id in model_ids:
        if not isinstance(model_id, str) or model_id not in _MODEL_BY_ID:
            raise ValueError("所选模型不能参与语言风险检测")
        if model_id in selected:
            raise ValueError("不能重复选择同一个风险模型")
        selected.append(model_id)
    if mode == "vote" and not 2 <= len(selected) <= 3:
        raise ValueError("投票模式需要选择 2 到 3 个不同的风险模型")
    if mode == "single" and len(selected) != 1:
        raise ValueError("单模型模式需要选择 1 个风险模型")
    return message, selected


def _risk_level(score):
    return "low" if score < 40 else "medium" if score < 70 else "high"


def _validate_output(output):
    try:
        score, reason = output
        if isinstance(score, bool):
            raise ValueError
        score = float(score)
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise CheckError("模型返回了无效的风险分数") from None
    if not isinstance(reason, str) or not reason.strip():
        raise CheckError("模型未返回有效的风险说明")
    return score, _risk_level(score), reason.strip()


def _check_member(message, model_id, resolved=None, source=None, display_name=None):
    started = perf_counter()
    member = {
        "id": model_id,
        "display_name": display_name or _MODEL_BY_ID[model_id]["display_name"],
        "source": source or "local",
        "source_label": _SOURCE_LABELS.get(source or "local", "本地 Ollama"),
        "status": "error",
        "score": None,
        "level": None,
        "label": "检测失败",
        "reason": "",
        "error": None,
        "elapsed_seconds": 0.0,
    }
    try:
        try:
            if resolved is None:
                model = checkmodel.get_model(model_id, source=source) if model_id == "deepseek_r1" and source else checkmodel.get_model(model_id)
            else:
                model, lookup_error = resolved
                if lookup_error is not None:
                    raise lookup_error
        except KeyError:
            if model_id == "deepseek_r1" and source == "cloud":
                error = "所选 DeepSeek 云端 API 当前不可用，请检查 API 密钥配置"
            elif model_id == "deepseek_r1" and source == "local":
                error = "所选 DeepSeek 本地 Ollama 当前不可用，请检查本地服务和模型权重"
            else:
                error = "该模型当前不可用，请检查本地服务和模型权重"
            member.update(
                status="unavailable", label="模型不可用",
                error=error,
            )
            return member
        display_name = getattr(model, "display_name", None)
        if isinstance(display_name, str) and display_name.strip():
            member["display_name"] = display_name
        score, level, reason = _validate_output(model.check(message))
        member.update(
            status="ok", score=score, level=level,
            label=_LEVEL_LABELS[level], reason=reason,
        )
    except RiskAbstention as exc:
        member.update(
            status="abstained", label="无法判断",
            reason=str(exc).strip() or "模型无法评估这条消息的语言风险",
        )
    except CheckError as exc:
        member["error"] = str(exc).strip() or "模型检测失败，请稍后重试"
    except Exception:
        member["error"] = "模型检测失败，请稍后重试"
    finally:
        member["elapsed_seconds"] = round(perf_counter() - started, 3)
    return member


def _aggregate_result(members, selected, mode, started):
    """按选定成员总数计票；无多数则对有效分数取均值，零有效分才无法判断。"""
    votes = {"low": 0, "medium": 0, "high": 0}
    for member in members:
        if member["status"] == "ok":
            votes[member["level"]] += 1

    success_count = sum(votes.values())
    agreement_count = max(votes.values())
    majority_required = len(selected) // 2 + 1 if mode == "vote" else 1
    level = "uncertain"
    decision_method = "unavailable"
    mean_score = None
    fallback_reason = ""
    if mode == "single" and success_count:
        level = members[0]["level"]
        decision_method = "single"
    elif agreement_count >= majority_required and success_count >= majority_required:
        level = max(votes, key=votes.get)
        decision_method = "majority"
    elif success_count:
        valid_scores = [member["score"] for member in members if member["status"] == "ok"]
        mean_score = math.fsum(valid_scores) / len(valid_scores)
        level = _risk_level(mean_score)
        decision_method = "mean_fallback"
        if success_count < majority_required:
            fallback_reason = "有效票不足（{}/{}），未达到 {} 票的多数门槛".format(
                success_count, len(selected), majority_required,
            )
        else:
            fallback_reason = "票数分歧，未形成多数风险等级"

    if decision_method == "unavailable":
        warning = "本次检测没有获得有效风险分数，无法判断。"
    elif decision_method == "mean_fallback":
        warning = "{}；已降级采用 {} 个有效模型风险分的算术平均值，结果为{}。".format(
            fallback_reason, success_count, _LEVEL_LABELS[level],
        )
    else:
        warning = "{}结果为{}。".format(
            "多数投票" if decision_method == "majority" else "单模型检测", _LEVEL_LABELS[level],
        )
    return {
        "mode": mode,
        "level": level,
        "label": _LEVEL_LABELS[level],
        "warning": warning,
        "decision_method": decision_method,
        "mean_score": mean_score,
        "fallback_reason": fallback_reason,
        "votes": votes,
        "majority_required": majority_required,
        "selected_count": len(selected),
        "success_count": success_count,
        "agreement_count": agreement_count,
        "has_failures": any(
            member["status"] in ("error", "unavailable") for member in members
        ),
        "elapsed_seconds": round(perf_counter() - started, 3),
        "members": members,
    }


def iter_risk_check(message, model_ids, mode="vote", deepseek_source=None, cloud_model=None):
    """逐个模型发出真实开始、完成事件，最后给出与同步检测相同的聚合结果。"""
    message, selected = validate_risk_request(message, model_ids, mode, deepseek_source)
    if cloud_model is not None and deepseek_source is None and "deepseek_r1" in selected:
        # Web callers always supply a personal model or an unavailable sentinel.
        # Only inspect the local source here; never resolve the environment-backed cloud instance.
        if getattr(cloud_model, "credential_mode", None) == "expired":
            deepseek_source = "cloud"
        else:
            local_available = any(source["source"] == "local" and source["available"]
                                  for source in checkmodel.get_model_sources("deepseek_r1"))
            deepseek_source = "cloud" if cloud_model.detect() or not local_available else "local"
    started = perf_counter()
    members = []
    for model_id in selected:
        model = None
        lookup_error = None
        try:
            if model_id == "deepseek_r1" and cloud_model is not None and deepseek_source in (None, "cloud"):
                model = cloud_model
            elif model_id == "deepseek_r1" and deepseek_source is not None:
                model = checkmodel.get_model(model_id, source=deepseek_source)
            else:
                model = checkmodel.get_model(model_id)
        except Exception as exc:
            lookup_error = exc
        source = deepseek_source if model_id == "deepseek_r1" else "local"
        if source is None:
            actual_source = getattr(model, "source", None)
            source = actual_source if isinstance(actual_source, str) and actual_source in DEEPSEEK_SOURCES else "cloud"
        display_name = getattr(model, "display_name", None)
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = "DeepSeek（云端 API）" if model_id == "deepseek_r1" and source == "cloud" else _MODEL_BY_ID[model_id]["display_name"]
        credential_mode = getattr(model, "credential_mode", "default") if source == "cloud" else "local"
        if credential_mode not in ("default", "personal", "expired", "missing", "local"):
            credential_mode = "default" if source == "cloud" else "local"
        yield {
            "type": "member_start",
            "member": {
                "id": model_id,
                "display_name": display_name,
                "source": source,
                "source_label": _SOURCE_LABELS[source],
                "credential_mode": credential_mode,
                "status": "running",
            },
        }
        member = _check_member(message, model_id, resolved=(model, lookup_error), source=source, display_name=display_name)
        member["credential_mode"] = credential_mode
        members.append(member)
        yield {"type": "member_complete", "member": member}
    yield {
        "type": "complete",
        "result": _aggregate_result(members, selected, mode, started),
    }


def run_risk_check(message, model_ids, mode="vote", deepseek_source=None, cloud_model=None):
    """同步调用同一检测流程，保留既有函数签名和完整结果字段。"""
    for event in iter_risk_check(message, model_ids, mode, deepseek_source, cloud_model=cloud_model):
        if event["type"] == "complete":
            return event["result"]
    raise RuntimeError("风险检测未产生完整结果")
