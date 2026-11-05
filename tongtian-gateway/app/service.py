"""协议无关的请求执行：路由、配额、故障转移、用量结算。

三个协议面只做请求/响应翻译，真正的调用都走这里，保证配额与账本口径一致。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from .adapters.base import (
    AdapterError,
    AdapterSet,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    StreamError,
    StreamEvent,
    StreamStart,
    StreamStop,
    TextDelta,
    Usage,
    UsageEvent,
)
from .auth import Principal
from .config import Settings
from .contract import UsageEvent as ContractUsageEvent
from .db import now_ms
from .errors import ERROR_SPECS, GatewayError
from .policy import PolicyStore
from .providers_store import ProviderStore
from .quota import reset_at
from .quota_sqlite import Reservation, SqliteQuota
from .routing import ModelCandidate, RoutePlan, quota_limits, resolve_plan
from .tpm import TpmBucket, TpmLimiter, TpmReservation
from .usage_store import (
    UsageStore,
    compute_cost,
    estimate_completion_tokens,
)

logger = logging.getLogger("tongtian.gateway.service")

FALLBACK_CODES = frozenset(
    {"PROVIDER_UNAVAILABLE", "UPSTREAM_TIMEOUT", "UPSTREAM_RATE_LIMITED"}
)


@dataclass
class Execution:
    request_id: str
    principal: Principal
    protocol: str
    endpoint: str
    stream: bool
    started_ms: int
    plan: RoutePlan
    reservation: Reservation
    limits: dict
    prompt_estimate: int
    max_output: int
    project_id: str | None
    tpm: TpmReservation | None = None
    headers: dict[str, str] = field(default_factory=dict)
    done: bool = False


class ExecutionService:
    def __init__(
        self,
        settings: Settings,
        policy_store: PolicyStore,
        providers: ProviderStore,
        quota: SqliteQuota,
        usage_store: UsageStore,
        adapters: AdapterSet,
        tpm: TpmLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._policy_store = policy_store
        self._providers = providers
        self._quota = quota
        self._usage = usage_store
        self._adapters = adapters
        self._tpm = tpm or TpmLimiter()

    def policy(self, tenant: str | None = None) -> dict:
        return self._policy_store.snapshot(tenant).document

    def begin_chat(
        self,
        principal: Principal,
        model: str,
        *,
        max_tokens: int | None,
        prompt_estimate: int,
        request_id: str,
        endpoint: str,
        stream: bool,
        project_id: str | None = None,
        protocol: str = "openai",
    ) -> Execution:
        policy = self.policy(principal.tenant)
        plan = resolve_plan(policy, principal, model)
        cap = plan.first.max_output_tokens
        if max_tokens is None:
            effective = cap
        else:
            if max_tokens > cap:
                raise GatewayError.of(
                    "INVALID_REQUEST",
                    f"max_tokens={max_tokens} 超过当前策略上限 {cap}",
                    param="max_tokens",
                )
            effective = max_tokens
        limits = self._quota.effective_limits(
            quota_limits(policy, principal),
            principal.tenant,
            principal.department,
            principal.subject,
        )
        reservation = self._quota.reserve(
            principal.quota_key,
            limits,
            max(1, prompt_estimate) + effective,
            api_key_id=principal.api_key_id,
            api_key_monthly=principal.api_key_quota_monthly_tokens,
        )
        try:
            tpm = self._reserve_tpm(
                principal,
                limits,
                max(1, prompt_estimate) + min(effective, cap),
            )
        except GatewayError:
            self._quota.release(reservation)
            raise
        return self._execution(
            principal,
            plan,
            reservation,
            limits,
            prompt_estimate,
            effective,
            request_id,
            endpoint,
            stream,
            project_id,
            protocol,
            tpm,
        )

    def begin_embedding(
        self,
        principal: Principal,
        model: str,
        *,
        prompt_estimate: int,
        request_id: str,
        endpoint: str,
        project_id: str | None = None,
    ) -> Execution:
        policy = self.policy(principal.tenant)
        plan = resolve_plan(policy, principal, model, kind="embedding")
        limits = self._quota.effective_limits(
            quota_limits(policy, principal),
            principal.tenant,
            principal.department,
            principal.subject,
        )
        reservation = self._quota.reserve(
            principal.quota_key,
            limits,
            max(1, prompt_estimate),
            api_key_id=principal.api_key_id,
            api_key_monthly=principal.api_key_quota_monthly_tokens,
        )
        try:
            tpm = self._reserve_tpm(principal, limits, max(1, prompt_estimate))
        except GatewayError:
            self._quota.release(reservation)
            raise
        return self._execution(
            principal,
            plan,
            reservation,
            limits,
            prompt_estimate,
            0,
            request_id,
            endpoint,
            False,
            project_id,
            "openai",
            tpm,
        )

    async def complete_chat(
        self,
        execution: Execution,
        factory: Callable[[Any, int], ChatRequest],
    ) -> tuple[ChatResponse, ModelCandidate, dict[str, str]]:
        last_code: str | None = None
        last_message = "上游模型服务不可用"
        last_candidate = execution.plan.first

        for index, candidate in enumerate(execution.plan.chain):
            last_candidate = candidate
            try:
                target = self._target(candidate)
            except GatewayError as exc:
                last_code, last_message = exc.code, exc.message
                if self._can_fallback(execution, index, exc.code):
                    continue
                headers = self._finish(execution, candidate, Usage(), "error", exc.code)
                raise GatewayError.of(exc.code, exc.message, headers=headers) from None
            request = factory(target, min(execution.max_output, candidate.max_output_tokens))
            try:
                response = await self._adapters.chat.complete(request)
            except AdapterError as exc:
                last_code, last_message = exc.code, exc.message
                if self._can_fallback(execution, index, exc.code, exc.retryable):
                    continue
                headers = self._finish(execution, candidate, Usage(), "error", exc.code)
                error = exc.to_gateway_error()
                error.headers = headers
                raise error from None
            headers = self._finish(execution, candidate, response.usage, "ok", None)
            return response, candidate, headers

        headers = self._finish(
            execution,
            last_candidate,
            Usage(),
            "error",
            last_code or "PROVIDER_UNAVAILABLE",
        )
        raise GatewayError.of(
            last_code or "PROVIDER_UNAVAILABLE", last_message, headers=headers
        )

    async def stream_chat(
        self,
        execution: Execution,
        factory: Callable[[Any, int], ChatRequest],
    ) -> AsyncIterator[StreamEvent]:
        emitted = False
        finished = False
        texts: list[str] = []
        usage: Usage | None = None
        candidate: ModelCandidate | None = None
        last_code: str | None = None
        last_message = "上游模型服务不可用"

        try:
            for index, item in enumerate(execution.plan.chain):
                candidate = item
                try:
                    target = self._target(item)
                except GatewayError as exc:
                    last_code, last_message = exc.code, exc.message
                    if self._can_fallback(execution, index, exc.code):
                        continue
                    self._finish(execution, item, Usage(), "error", exc.code)
                    raise GatewayError.of(exc.code, exc.message) from None
                request = factory(target, min(execution.max_output, item.max_output_tokens))
                broke_for_fallback = False
                async for event in self._adapters.chat.stream(request):
                    if isinstance(event, StreamError):
                        last_code, last_message = event.code, event.message
                        if not emitted and self._can_fallback(
                            execution, index, event.code, event.retryable
                        ):
                            broke_for_fallback = True
                            break
                        settled = self._interrupted_usage(
                            execution, usage, emitted, texts
                        )
                        self._finish(execution, item, settled, "error", event.code)
                        finished = True
                        if not emitted:
                            raise GatewayError.of(event.code, event.message) from None
                        yield event
                        return
                    if not isinstance(event, UsageEvent):
                        emitted = True
                    if isinstance(event, TextDelta):
                        texts.append(event.text)
                    if isinstance(event, UsageEvent):
                        usage = event.usage
                    yield event
                    if isinstance(event, StreamStop):
                        final_usage = usage or self._estimated_usage(
                            execution, "".join(texts)
                        )
                        self._finish(execution, item, final_usage, "ok", None)
                        finished = True
                        return
                if broke_for_fallback:
                    continue
                # 上游结束但没有结束帧
                if not emitted and self._can_fallback(
                    execution, index, "UPSTREAM_ERROR"
                ):
                    last_code, last_message = "UPSTREAM_ERROR", "上游流式响应意外结束"
                    continue
                last_code, last_message = "UPSTREAM_ERROR", "上游流式响应意外结束"
                settled = self._interrupted_usage(execution, usage, emitted, texts)
                self._finish(execution, item, settled, "error", "UPSTREAM_ERROR")
                finished = True
                if not emitted:
                    raise GatewayError.of("UPSTREAM_ERROR", last_message) from None
                yield StreamError(
                    code="UPSTREAM_ERROR",
                    message=last_message,
                    retryable=ERROR_SPECS["UPSTREAM_ERROR"].retryable,
                )
                return

            settled = self._interrupted_usage(execution, usage, emitted, texts)
            headers = self._finish(
                execution,
                candidate,
                settled,
                "error",
                last_code or "PROVIDER_UNAVAILABLE",
            )
            raise GatewayError.of(
                last_code or "PROVIDER_UNAVAILABLE", last_message, headers=headers
            )
        except (asyncio.CancelledError, GeneratorExit):
            if not finished:
                settled = self._interrupted_usage(execution, usage, emitted, texts)
                self._finish(execution, candidate, settled, "cancelled", "CLIENT_CANCELLED")
            raise

    async def embed(
        self,
        execution: Execution,
        factory: Callable[[Any], EmbeddingRequest],
    ) -> tuple[EmbeddingResponse, ModelCandidate, dict[str, str]]:
        candidate = execution.plan.first
        last_code = "PROVIDER_UNAVAILABLE"
        last_message = "上游模型服务不可用"
        for index, item in enumerate(execution.plan.chain):
            candidate = item
            try:
                target = self._target(item)
            except GatewayError as exc:
                last_code, last_message = exc.code, exc.message
                if self._can_fallback(execution, index, exc.code):
                    continue
                headers = self._finish(execution, item, Usage(), "error", exc.code)
                raise GatewayError.of(exc.code, exc.message, headers=headers) from None
            try:
                response = await self._adapters.embedding.embed(factory(target))
            except AdapterError as exc:
                last_code, last_message = exc.code, exc.message
                if self._can_fallback(execution, index, exc.code, exc.retryable):
                    continue
                headers = self._finish(execution, item, Usage(), "error", exc.code)
                error = exc.to_gateway_error()
                error.headers = headers
                raise error from None
            headers = self._finish(execution, item, response.usage, "ok", None)
            return response, item, headers
        headers = self._finish(execution, candidate, Usage(), "error", last_code)
        raise GatewayError.of(last_code, last_message, headers=headers)

    # ---- 内部 ----

    def _execution(
        self,
        principal: Principal,
        plan: RoutePlan,
        reservation: Reservation,
        limits: dict,
        prompt_estimate: int,
        max_output: int,
        request_id: str,
        endpoint: str,
        stream: bool,
        project_id: str | None,
        protocol: str = "openai",
        tpm: TpmReservation | None = None,
    ) -> Execution:
        execution = Execution(
            request_id=request_id,
            principal=principal,
            protocol=protocol,
            endpoint=endpoint,
            stream=stream,
            started_ms=now_ms(),
            plan=plan,
            reservation=reservation,
            limits=limits,
            prompt_estimate=prompt_estimate,
            max_output=max_output,
            project_id=project_id,
            tpm=tpm,
        )
        execution.headers = self._headers(execution, plan.first, self._remaining(principal, limits))
        return execution

    def _reserve_tpm(
        self, principal: Principal, limits: dict, cost: int
    ) -> TpmReservation:
        principal_tpm = limits.get("tpm")
        if isinstance(principal_tpm, bool) or not isinstance(principal_tpm, int):
            principal_tpm = 0
        return self._tpm.reserve(
            (
                TpmBucket("principal", principal.quota_key, principal_tpm),
                TpmBucket(
                    "key",
                    principal.api_key_id or "",
                    principal.api_key_rate_limit_tpm,
                ),
            ),
            cost,
        )

    def _target(self, candidate: ModelCandidate):
        try:
            return self._providers.resolve_target(
                candidate.provider,
                model=candidate.model,
                upstream_model=candidate.upstream_model,
            )
        except GatewayError as exc:
            if exc.code == "INVALID_REQUEST":
                raise GatewayError.of(
                    "PROVIDER_UNAVAILABLE", f"provider {candidate.provider} 未配置"
                ) from None
            raise

    def _can_fallback(
        self,
        execution: Execution,
        index: int,
        code: str,
        retryable: bool | None = None,
    ) -> bool:
        if not execution.plan.fallback_allowed:
            return False
        if index + 1 >= len(execution.plan.chain):
            return False
        if retryable is not None:
            return retryable
        return code in FALLBACK_CODES

    def _remaining(self, principal: Principal, limits: dict) -> int | None:
        try:
            return self._quota.remaining(principal.quota_key, limits)
        except Exception:  # noqa: BLE001 - 头信息失败不影响响应
            logger.exception("读取剩余配额失败")
            return None

    def _headers(
        self, execution: Execution, candidate: ModelCandidate, remaining: int | None
    ) -> dict[str, str]:
        snapshot = self._policy_store.snapshot(execution.principal.tenant)
        headers = {
            "X-TT-Request-Id": execution.request_id,
            "X-TT-Model": candidate.model,
            "X-TT-Quota-Reset": reset_at(),
        }
        if execution.plan.alias:
            headers["X-TT-Model-Alias"] = execution.plan.alias
        if snapshot.policy_version:
            headers["X-TT-Policy-Version"] = snapshot.policy_version
        if snapshot.stale:
            headers["X-TT-Policy-Stale"] = "1"
        if remaining is not None:
            headers["X-TT-Quota-Remaining"] = str(remaining)
        return headers

    def _estimated_usage(self, execution: Execution, text: str) -> Usage:
        return Usage(
            prompt_tokens=max(1, execution.prompt_estimate),
            completion_tokens=estimate_completion_tokens(text),
            estimated=True,
        )

    def _interrupted_usage(
        self,
        execution: Execution,
        usage: Usage | None,
        emitted: bool,
        texts: list[str],
    ) -> Usage:
        # 上游还没开始回包就失败，说明没消费 token，不能按估算记账；
        # 已经吐过内容后再中断，才按已产生的输出估算。
        if usage is not None:
            return usage
        if emitted:
            return self._estimated_usage(execution, "".join(texts))
        return Usage()

    def _finish(
        self,
        execution: Execution,
        candidate: ModelCandidate | None,
        usage: Usage,
        status: str,
        error_code: str | None,
    ) -> dict[str, str]:
        if execution.done:
            return execution.headers
        execution.done = True
        try:
            self._quota.settle(execution.reservation, usage.total_tokens)
        except Exception:  # noqa: BLE001 - 结算失败也要回包，账本问题记日志
            logger.exception("配额结算失败 request=%s", execution.request_id)
        try:
            self._tpm.refund(execution.tpm, usage.total_tokens)
        except Exception:  # noqa: BLE001 - TPM 退回失败不影响回包
            logger.exception("TPM 退回失败 request=%s", execution.request_id)
        cost = (
            compute_cost(candidate.pricing, usage)
            if candidate is not None
            else compute_cost(None, usage)
        )
        finished_at = now_ms()
        event = ContractUsageEvent(
            gatewayId=self._settings.gateway_id,
            requestId=execution.request_id,
            tenantId=execution.principal.tenant,
            departmentId=execution.principal.department or None,
            userId=execution.principal.subject or None,
            apiKeyId=execution.principal.api_key_id,
            projectId=execution.project_id,
            origin="gateway",
            protocol=execution.protocol,
            endpoint=execution.endpoint,
            modelAlias=execution.plan.alias,
            model=candidate.model if candidate else execution.plan.first.model,
            provider=candidate.provider if candidate else execution.plan.first.provider,
            promptTokens=usage.prompt_tokens,
            completionTokens=usage.completion_tokens,
            cachedTokens=usage.cached_tokens,
            reasoningTokens=usage.reasoning_tokens,
            totalTokens=usage.total_tokens,
            cost=cost.amount,
            currency=cost.currency,
            priceVersion=candidate.price_version if candidate else None,
            latencyMs=max(0, finished_at - execution.started_ms),
            status=status,
            errorCode=error_code,
            stream=execution.stream,
            estimated=usage.estimated,
            startedAt=execution.started_ms,
            finishedAt=finished_at,
        )
        try:
            self._usage.record(event)
        except Exception:  # noqa: BLE001
            logger.exception("用量落库失败 request=%s", execution.request_id)
        remaining = self._remaining(execution.principal, execution.limits)
        execution.headers = self._headers(
            execution, candidate or execution.plan.first, remaining
        )
        return execution.headers
