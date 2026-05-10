from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class EndpointDigest(BaseModel):
    path: str
    method: str
    controller: str
    handler: str
    request_dto: Optional[str] = None
    response_dto: Optional[str] = None
    auth_required: bool = False
    roles: list[str] = Field(default_factory=list)


class EntityDigest(BaseModel):
    name: str
    table: str
    fields: list[str] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)


class FeignClientDigest(BaseModel):
    client_name: str
    target_service: str
    calls: list[str] = Field(default_factory=list)


class EventDigest(BaseModel):
    produces: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)


class ServiceDigest(BaseModel):
    project: str
    type: str
    digest_version: str = "1.0"
    created_at: str
    endpoints: list[EndpointDigest] = Field(default_factory=list)
    entities: list[EntityDigest] = Field(default_factory=list)
    dtos: list[str] = Field(default_factory=list)
    feign_clients: list[FeignClientDigest] = Field(default_factory=list)
    events: EventDigest = Field(default_factory=EventDigest)
    security_config: dict = Field(default_factory=dict)


class AngularComponentDigest(BaseModel):
    name: str
    selector: str
    file_path: str
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    injected_services: list[str] = Field(default_factory=list)


class AngularServiceDigest(BaseModel):
    name: str
    file_path: str
    http_calls: list[dict] = Field(default_factory=list)
    injected_dependencies: list[str] = Field(default_factory=list)


class AngularDigest(BaseModel):
    project: str
    type: str = "angular"
    digest_version: str = "1.0"
    created_at: str
    modules: list[str] = Field(default_factory=list)
    components: list[AngularComponentDigest] = Field(default_factory=list)
    services: list[AngularServiceDigest] = Field(default_factory=list)
    routes: list[dict] = Field(default_factory=list)
    guards: list[str] = Field(default_factory=list)
    interceptors: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)


class ApiContract(BaseModel):
    caller: str
    service: str
    endpoint: str
    angular_service: Optional[str] = None


class MasterDigest(BaseModel):
    system: str
    digest_version: str = "1.0"
    created_at: str
    projects: list[str] = Field(default_factory=list)
    api_contracts: list[ApiContract] = Field(default_factory=list)
    service_dependencies: dict[str, list[str]] = Field(default_factory=dict)
    auth_flow: dict = Field(default_factory=dict)
    shared_models: list[str] = Field(default_factory=list)
