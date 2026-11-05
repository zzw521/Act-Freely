"""按策略解析模型别名、部门白名单与输出上限。

`resolve_plan` 返回整条可用链路（供故障转移），`resolve_route`
保留单模型视图，给管理面与测试使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .auth import Principal
from .errors import GatewayError

CHAT = "chat"
EMBEDDING = "embeddings"


@dataclass(frozen=True)
class ModelCandidate:
    model: str
    provider: str
    upstream_model: str
    max_output_tokens: int
    pricing: Mapping | None = None
    price_version: str | None = None


@dataclass(frozen=True)
class RoutePlan:
    requested: str
    alias: str | None
    chain: tuple[ModelCandidate, ...]
    fallback_allowed: bool

    @property
    def first(self) -> ModelCandidate:
        return self.chain[0]


@dataclass(frozen=True)
class RouteResolution:
    requested: str
    alias: str | None
    chain: tuple[str, ...]
    model: str
    provider: str
    upstream_model: str
    max_output_tokens: int
    fallback_allowed: bool


def department_policy(policy: dict, principal: Principal) -> dict:
    departments = policy.get("departments") or {}
    return departments.get(principal.department) or {}


def allowed_models(policy: dict, principal: Principal) -> set[str]:
    department = department_policy(policy, principal)
    raw = department.get("allowedModels")
    if raw is None:
        raw = policy.get("allowedModels") or []
    return set(raw)


def quota_limits(policy: dict, principal: Principal) -> dict:
    limits = dict(policy.get("quotaDefaults") or {})
    limits.update(department_policy(policy, principal).get("quota") or {})
    return limits


def model_visible(principal: Principal, name: str) -> bool:
    """API Key 的模型白名单只过滤“请求名”（别名或具体模型）。"""
    return not principal.model_allowlist or name in principal.model_allowlist


def _max_output_tokens(model: dict, limits: dict, fallback: int | None) -> int:
    values = [
        value
        for value in (model.get("maxOutputTokens"), limits.get("maxOutputTokens"))
        if isinstance(value, int) and value > 0
    ]
    if values:
        return min(values)
    return fallback or 8192


def _supports(model: dict, capability: str) -> bool:
    return capability in (model.get("capabilities") or [])


def _candidate(policy: dict, name: str, spec: dict, limits: dict) -> ModelCandidate:
    pricing = spec.get("pricing")
    return ModelCandidate(
        model=name,
        provider=str(spec.get("provider") or ""),
        upstream_model=str(spec.get("upstreamModel") or name),
        max_output_tokens=_max_output_tokens(
            spec, limits, (policy.get("quotaDefaults") or {}).get("maxOutputTokens")
        ),
        pricing=dict(pricing) if isinstance(pricing, Mapping) else None,
        price_version=spec.get("priceVersion"),
    )


def _alias_chain(
    policy: dict, principal: Principal, alias: str, capability: str
) -> tuple[ModelCandidate, ...]:
    route = (policy.get("routes") or {}).get(alias) or {}
    models = policy.get("models") or {}
    allowed = allowed_models(policy, principal)
    limits = quota_limits(policy, principal)
    chain = tuple(
        _candidate(policy, name, models[name], limits)
        for name in (route.get("chain") or [])
        if name in models and name in allowed and _supports(models[name], capability)
    )
    if not chain:
        raise GatewayError.of(
            "MODEL_NOT_ALLOWED", f"别名 {alias} 在当前部门没有可用模型"
        )
    return chain


def _concrete(
    policy: dict, principal: Principal, model: str, capability: str
) -> tuple[ModelCandidate, ...]:
    models = policy.get("models") or {}
    spec = models.get(model)
    if spec is None:
        raise GatewayError.of("MODEL_NOT_ALLOWED", f"未知模型：{model}")
    if model not in allowed_models(policy, principal):
        raise GatewayError.of("MODEL_NOT_ALLOWED", f"模型 {model} 不在当前部门白名单")
    if not _supports(spec, capability):
        raise GatewayError.of("MODEL_NOT_ALLOWED", f"模型 {model} 不支持 {capability}")
    return (_candidate(policy, model, spec, quota_limits(policy, principal)),)


def resolve_plan(
    policy: dict,
    principal: Principal,
    requested: str,
    *,
    kind: str = "chat",
) -> RoutePlan:
    if not requested:
        raise GatewayError.of("INVALID_REQUEST", "缺少 model 参数", param="model")
    if not model_visible(principal, requested):
        raise GatewayError.of(
            "MODEL_NOT_ALLOWED", f"模型 {requested} 不在当前 API Key 的白名单"
        )
    capability = EMBEDDING if kind == "embedding" else CHAT
    routes = policy.get("routes") or {}
    if kind == "embedding":
        embedding_route = policy.get("embeddingRoute")
        if not embedding_route or requested != embedding_route:
            raise GatewayError.of(
                "MODEL_NOT_ALLOWED",
                f"嵌入请求必须使用 embeddingRoute：{embedding_route or '(未配置)'}",
            )
        if embedding_route not in routes:
            raise GatewayError.of("MODEL_NOT_ALLOWED", "embeddingRoute 未在 routes 中定义")
        chain = _alias_chain(policy, principal, embedding_route, capability)
        return RoutePlan(
            requested=requested,
            alias=embedding_route,
            chain=chain,
            fallback_allowed=bool(routes[embedding_route].get("fallbackAllowed", True)),
        )
    if requested in routes:
        chain = _alias_chain(policy, principal, requested, capability)
        return RoutePlan(
            requested=requested,
            alias=requested,
            chain=chain,
            fallback_allowed=bool(routes[requested].get("fallbackAllowed", True)),
        )
    chain = _concrete(policy, principal, requested, capability)
    return RoutePlan(
        requested=requested, alias=None, chain=chain, fallback_allowed=False
    )


def _resolution(plan: RoutePlan) -> RouteResolution:
    first = plan.first
    return RouteResolution(
        requested=plan.requested,
        alias=plan.alias,
        chain=tuple(item.model for item in plan.chain),
        model=first.model,
        provider=first.provider,
        upstream_model=first.upstream_model,
        max_output_tokens=first.max_output_tokens,
        fallback_allowed=plan.fallback_allowed,
    )


def resolve_route(
    policy: dict,
    principal: Principal,
    requested: str,
    *,
    kind: str = "chat",
) -> RouteResolution:
    return _resolution(resolve_plan(policy, principal, requested, kind=kind))
