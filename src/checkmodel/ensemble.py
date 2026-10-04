"""语言风险等级优先等权投票，无多数时按有效分数均值分档。"""

import math
from time import perf_counter

import checkmodel

from .base import CheckError, RiskAbstention


MAX_MESSAGE_LENGTH = 6000
_LEVEL_LABELS = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
    "uncertain": "无法判断",
}


def get_registered_risk_models(refresh=False):
    """风险候选来自模型工厂的登记协议，不依赖型号或当前连接状态。"""
    registered = (checkmodel.get_registered_models(score_kind="risk", refresh=True)
                  if refresh else checkmodel.get_registered_models(score_kind="risk"))
    return [
        dict(model) for model in registered
        if model.get("score_kind") == "risk"
    ]


def get_risk_models(refresh=False):
    """只探测已登记的风险适配器，名单保持登记顺序且不修改登记信息。"""
    registered = get_registered_risk_models(refresh=refresh)
    model_ids = [model["id"] for model in registered]
    available = (checkmodel.get_models(model_ids=model_ids, refresh=True)
                 if refresh else checkmodel.get_models(model_ids=model_ids))
    available_ids = {
        model["id"] for model in available
        if model.get("available", True) and model.get("score_kind", "risk") == "risk"
    }
    return [
        {**model, "available": True}
        for model in registered if model["id"] in available_ids
    ]


def _validate_request(message, model_ids, mode):
    if not isinstance(message, str) or not message.strip():
        raise ValueError("请输入消息内容")
    message = message.strip()
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ValueError("消息内容不能超过 {} 个字符".format(MAX_MESSAGE_LENGTH))
    if mode not in ("vote", "single"):
        raise ValueError("请选择有效的检测模式")
    if not isinstance(model_ids, (list, tuple)):
        raise ValueError("请选择检测模型")

    registered_ids = {model["id"] for model in get_registered_risk_models()}
    selected = []
    for model_id in model_ids:
        if not isinstance(model_id, str) or model_id not in registered_ids:
            raise ValueError("所选模型不能参与语言风险检测")
        if model_id in selected:
            raise ValueError("不能重复选择同一个风险模型")
        selected.append(model_id)
    if mode == "vote" and not 2 <= len(selected) <= 3:
        raise ValueError("投票模式需要选择 2 到 3 个不同的风险模型")
    if mode == "single" and len(selected) != 1:
        raise ValueError("单模型模式需要选择 1 个风险模型")
    return message, selected


def validate_risk_request(message, model_ids, mode="vote"):
    """同步与流式检测共用的输入校验；此处不会获取或运行模型。"""
    return _validate_request(message, model_ids, mode)


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


def _empty_member(model_id, metadata, status="error"):
    return {
        "id": model_id,
        "display_name": metadata["display_name"],
        "status": status,
        "score": None,
        "level": None,
        "label": "正在分析" if status == "running" else "检测失败",
        "reason": "",
        "error": None,
        "elapsed_seconds": 0.0,
    }


def _check_member(message, model_id, metadata):
    started = perf_counter()
    member = _empty_member(model_id, metadata)
    try:
        try:
            model = checkmodel.get_model(model_id)
        except KeyError:
            member.update(
                status="unavailable", label="模型不可用",
                error="该模型当前不可用，请检查本地服务和模型权重",
            )
            return member
        if getattr(model, "score_kind", "probability") != "risk":
            raise CheckError("所选模型不提供风险评分，不能参与风险检测")
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


def iter_risk_check(message, model_ids, mode="vote"):
    """先发送开始事件，再逐个调用本地模型；只有完整执行后才聚合结果。"""
    message, selected = validate_risk_request(message, model_ids, mode)
    metadata = {model["id"]: model for model in get_registered_risk_models()}
    started = perf_counter()
    members = []
    for model_id in selected:
        yield {"type": "member_start", "member": _empty_member(model_id, metadata[model_id], "running")}
        member = _check_member(message, model_id, metadata[model_id])
        members.append(member)
        yield {"type": "member_complete", "member": dict(member)}
    yield {"type": "complete", "result": _aggregate_result(members, selected, mode, started)}


def run_risk_check(message, model_ids, mode="vote"):
    """同步入口消费同一组真实模型事件，保证与流式检测的评分规则相同。"""
    for event in iter_risk_check(message, model_ids, mode):
        if event["type"] == "complete":
            return event["result"]
