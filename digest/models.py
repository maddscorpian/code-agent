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
    javadoc: str = ""


class EntityDigest(BaseModel):
    name: str
    table: str
    fields: list[str] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)


class DtoFieldDigest(BaseModel):
    name: str
    type: str
    required: bool = False
    json_property: str = ""       # value from @JsonProperty if different from field name
    validations: list[str] = Field(default_factory=list)   # @NotNull, @Size, etc.


class DtoDigest(BaseModel):
    name: str
    file_path: str = ""
    fields: list[DtoFieldDigest] = Field(default_factory=list)


class FeignCallDetail(BaseModel):
    method: str
    path: str
    request_dto: str = ""         # @RequestBody type
    response_dto: str = ""        # unwrapped return type
    path_params: list[str] = Field(default_factory=list)


class FeignClientDigest(BaseModel):
    client_name: str
    target_service: str
    calls: list[str] = Field(default_factory=list)
    call_details: list[FeignCallDetail] = Field(default_factory=list)
    resolved_url: str = ""
    url_property_key: str = ""
    oauth_scope: str = ""          # from @AuthorizationToken(scope=...) custom annotation


class KafkaTopicConfig(BaseModel):
    topic_name: str
    role: str                   # "producer" | "consumer"
    property_key: str = ""      # e.g. "spring.kafka.consumer.topic"
    group_id: str = ""          # spring.kafka.consumer.group-id
    publisher_endpoint: str = ""  # REST path if produced via a publisher endpoint, e.g. "POST /events/order"


class EventDigest(BaseModel):
    produces: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)


class BeanDigest(BaseModel):
    name: str
    bean_type: str  # service | repository | component | configuration | advice
    file_path: str
    dependencies: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    transactional_methods: list[str] = Field(default_factory=list)
    method_calls: dict[str, list[str]] = Field(default_factory=dict)  # {method: [dep.call(), ...]}
    queries: list[str] = Field(default_factory=list)                  # JPQL/SQL from @Query + derived query descriptions
    method_bodies: dict[str, str] = Field(default_factory=dict)       # {method: first 500 chars of body}


class ScheduledTaskDigest(BaseModel):
    class_name: str
    method: str
    schedule: str


class ExceptionHandlerDigest(BaseModel):
    advice_class: str
    handled_exceptions: list[str] = Field(default_factory=list)


class ServiceDigest(BaseModel):
    project: str
    type: str
    digest_version: str = "2.0"
    created_at: str
    endpoints: list[EndpointDigest] = Field(default_factory=list)
    entities: list[EntityDigest] = Field(default_factory=list)
    dtos: list[str] = Field(default_factory=list)
    dto_schemas: list[DtoDigest] = Field(default_factory=list)
    feign_clients: list[FeignClientDigest] = Field(default_factory=list)
    events: EventDigest = Field(default_factory=EventDigest)
    kafka_topics: list[KafkaTopicConfig] = Field(default_factory=list)
    security_config: dict = Field(default_factory=dict)
    beans: list[BeanDigest] = Field(default_factory=list)
    exception_handlers: list[ExceptionHandlerDigest] = Field(default_factory=list)
    scheduled_tasks: list[ScheduledTaskDigest] = Field(default_factory=list)
    build_dependencies: list[str] = Field(default_factory=list)
    db_migrations: list[str] = Field(default_factory=list)


class AngularComponentDigest(BaseModel):
    name: str
    selector: str
    file_path: str
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    injected_services: list[str] = Field(default_factory=list)
    template_events: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    method_calls: dict[str, list[str]] = Field(default_factory=dict)  # {method: [this.svc.call(), ...]}
    view_children: list[str] = Field(default_factory=list)


class AngularServiceDigest(BaseModel):
    name: str
    file_path: str
    http_calls: list[dict] = Field(default_factory=list)
    injected_dependencies: list[str] = Field(default_factory=list)


class NgRxFeature(BaseModel):
    name: str
    actions: list[str] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list)
    selectors: list[str] = Field(default_factory=list)


class AngularDigest(BaseModel):
    project: str
    type: str = "angular"
    digest_version: str = "2.0"
    created_at: str
    modules: list[str] = Field(default_factory=list)
    components: list[AngularComponentDigest] = Field(default_factory=list)
    services: list[AngularServiceDigest] = Field(default_factory=list)
    routes: list[dict] = Field(default_factory=list)
    guards: list[str] = Field(default_factory=list)
    interceptors: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    ngrx_features: list[NgRxFeature] = Field(default_factory=list)
    environments: dict = Field(default_factory=dict)


class ApiContract(BaseModel):
    caller: str
    service: str
    endpoint: str
    angular_service: Optional[str] = None


class MasterDigest(BaseModel):
    system: str
    digest_version: str = "2.0"
    created_at: str
    projects: list[str] = Field(default_factory=list)
    api_contracts: list[ApiContract] = Field(default_factory=list)
    service_dependencies: dict[str, list[str]] = Field(default_factory=dict)
    auth_flow: dict = Field(default_factory=dict)
    shared_models: list[str] = Field(default_factory=list)
    kafka_event_flow: dict = Field(default_factory=dict)
