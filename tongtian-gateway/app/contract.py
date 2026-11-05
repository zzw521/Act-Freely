"""契约数据模型：能力发现、策略文档、用量事件。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import CONTRACT_VERSION


AMOUNT_RE = r"^(0|[1-9]\d*)(\.\d{1,8})?$"


class ProtocolSurface(BaseModel):
    name: Literal["openai", "anthropic", "gemini"]
    endpoints: list[str]
    # 逐协议实测结果；顶层 features 是各协议的逻辑并集
    stream: bool = False
    tools: bool = False
    vision: bool = False
    embeddings: bool = False
    testedAt: str | None = None
    note: str | None = Field(default=None, max_length=200)


class ModelPricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    inputPer1M: str = Field(pattern=AMOUNT_RE)
    outputPer1M: str = Field(pattern=AMOUNT_RE)
    cachedInputPer1M: str | None = Field(default=None, pattern=AMOUNT_RE)
    reasoningAsOutput: bool = True


class GatewayFeatures(BaseModel):
    stream: bool
    tools: bool
    vision: bool
    embeddings: bool
    countTokens: bool
    promptCaching: bool


class GatewayLimits(BaseModel):
    maxRequestBytes: int = Field(gt=0)
    maxOutputTokens: int = Field(gt=0)
    upstreamTimeoutSeconds: int = Field(gt=0)
    streamIdleTimeoutSeconds: int = Field(gt=0)


class Capabilities(BaseModel):
    contractVersion: str = CONTRACT_VERSION
    gatewayVersion: str
    gatewayId: str
    protocols: list[ProtocolSurface]
    features: GatewayFeatures
    providers: list[str]
    limits: GatewayLimits
    policyVersion: str | None = None


class GatewayEndpoint(BaseModel):
    url: str


class ByokPolicy(BaseModel):
    allowed: bool = True
    hardQuota: bool = False


class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str
    upstreamModel: str
    capabilities: list[str] = Field(default_factory=list)
    contextWindow: int | None = None
    maxOutputTokens: int | None = None
    priceVersion: str | None = None
    pricing: ModelPricing | None = None


class RoutePolicy(BaseModel):
    chain: list[str]
    fallbackAllowed: bool = True


class QuotaDefaults(BaseModel):
    monthlyTokens: int | None = None
    concurrency: int | None = None
    rpm: int | None = None
    tpm: int | None = None
    maxOutputTokens: int | None = None


class DepartmentPolicy(BaseModel):
    allowedModels: list[str] | None = None
    quota: QuotaDefaults | None = None
    byokAllowed: bool | None = None


class PolicyDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    contractVersion: str
    policyVersion: str
    version: str | None = None
    tenant: str
    gateway: GatewayEndpoint
    noLog: bool = True
    embeddingRoute: str | None = None
    byok: ByokPolicy = Field(default_factory=ByokPolicy)
    models: dict[str, ModelPolicy]
    routes: dict[str, RoutePolicy]
    allowedModels: list[str]
    quotaDefaults: QuotaDefaults = Field(default_factory=QuotaDefaults)
    departments: dict[str, DepartmentPolicy] = Field(default_factory=dict)
    issuedAt: str | None = None
    expiresAt: str | None = None

    @model_validator(mode="after")
    def _version_matches(self) -> "PolicyDocument":
        if self.version is not None and self.version != self.policyVersion:
            raise ValueError("version 必须与 policyVersion 相同")
        return self


class UsageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contractVersion: str = CONTRACT_VERSION
    gatewayId: str
    requestId: str
    tenantId: str
    departmentId: str | None = None
    userId: str | None = None
    apiKeyId: str | None = None
    projectId: str | None = None
    origin: Literal["gateway", "engine", "desktop"] = "gateway"
    protocol: Literal["openai", "anthropic", "gemini"]
    endpoint: str
    modelAlias: str | None = None
    model: str
    provider: str
    promptTokens: int = Field(default=0, ge=0)
    completionTokens: int = Field(default=0, ge=0)
    cachedTokens: int = Field(default=0, ge=0)
    reasoningTokens: int = Field(default=0, ge=0)
    totalTokens: int = Field(default=0, ge=0)
    cost: str = "0"
    currency: str = "USD"
    priceVersion: str | None = None
    latencyMs: int = Field(ge=0)
    status: Literal["ok", "error", "cancelled"]
    errorCode: str | None = None
    stream: bool = False
    estimated: bool = False
    startedAt: int
    finishedAt: int

    @model_validator(mode="after")
    def _recompute_total(self) -> "UsageEvent":
        if self.finishedAt < self.startedAt:
            raise ValueError("finishedAt 不能早于 startedAt")
        self.totalTokens = self.promptTokens + self.completionTokens
        return self


class UsageBatch(BaseModel):
    contractVersion: str = CONTRACT_VERSION
    gatewayId: str
    batchId: str
    events: list[UsageEvent]
