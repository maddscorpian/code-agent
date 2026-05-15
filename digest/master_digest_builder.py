from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from .models import AngularDigest, ApiContract, MasterDigest, ServiceDigest


class MasterDigestBuilder:
    def __init__(self, service_digests: list[ServiceDigest], angular_digest: AngularDigest | None):
        self.service_digests = service_digests
        self.angular_digest = angular_digest

    def build(self) -> MasterDigest:
        api_contracts = self._build_api_contracts()
        deps = self._build_dependencies()
        auth_flow = self._build_auth_flow()
        shared_models = self._build_shared_models()
        kafka_event_flow = self._build_kafka_event_flow()
        projects = [d.project for d in self.service_digests]
        if self.angular_digest:
            projects.append(self.angular_digest.project)
        return MasterDigest(
            system="local-microservices-system",
            created_at=datetime.now(timezone.utc).isoformat(),
            projects=projects,
            api_contracts=api_contracts,
            service_dependencies=deps,
            auth_flow=auth_flow,
            shared_models=shared_models,
            kafka_event_flow=kafka_event_flow,
        )

    def _build_api_contracts(self) -> list[ApiContract]:
        if not self.angular_digest:
            return []
        rows = []
        for svc in self.angular_digest.services:
            for call in svc.http_calls:
                url = str(call.get("url", ""))
                method = str(call.get("method", "GET"))
                for backend in self.service_digests:
                    for ep in backend.endpoints:
                        if ep.path and ep.path.strip("/") in url:
                            rows.append(
                                ApiContract(
                                    caller=self.angular_digest.project,
                                    service=backend.project,
                                    endpoint=f"{ep.method} {ep.path}",
                                    angular_service=f"{svc.name}.{method.lower()}",
                                )
                            )
        return rows

    def _build_dependencies(self) -> dict[str, list[str]]:
        deps: dict[str, set[str]] = defaultdict(set)
        produces_by_service = {s.project: set(s.events.produces) for s in self.service_digests}
        for svc in self.service_digests:
            for fc in svc.feign_clients:
                deps[svc.project].add(fc.target_service)
            for event in svc.events.consumes:
                for producer, topics in produces_by_service.items():
                    if event in topics and producer != svc.project:
                        deps[svc.project].add(producer)
        return {k: sorted(v) for k, v in deps.items()}

    def _build_auth_flow(self) -> dict:
        issuer = None
        validated_by = []
        for svc in self.service_digests:
            has_jwt = bool(svc.security_config.get("jwt_filter_present"))
            auth_ep = any("/auth" in ep.path or "login" in ep.path for ep in svc.endpoints)
            if auth_ep and issuer is None:
                issuer = svc.project
            if has_jwt:
                validated_by.append(svc.project)
        fe_interceptor = None
        if self.angular_digest:
            for i in self.angular_digest.interceptors:
                if "auth" in i.lower() or "jwt" in i.lower():
                    fe_interceptor = i
                    break
        return {
            "type": "JWT",
            "token_issuer": issuer,
            "validated_by": sorted(set(validated_by)),
            "fe_interceptor": fe_interceptor,
        }

    def _build_kafka_event_flow(self) -> dict:
        """
        Build a cross-service Kafka event flow map.
        Returns: {topic_name: {producers: [...], consumers: [...]}}
        Each producer/consumer entry: {service, endpoint (if via REST), group_id}
        """
        flow: dict[str, dict] = {}

        for svc in self.service_digests:
            project = svc.project

            # Use structured kafka_topics if available; fall back to raw events
            producer_topics: list[dict] = []
            consumer_topics: list[dict] = []

            if svc.kafka_topics:
                for kt in svc.kafka_topics:
                    entry = {
                        "service": project,
                        "property_key": kt.property_key,
                        "group_id": kt.group_id,
                    }
                    if kt.role in ("producer", "both"):
                        if kt.publisher_endpoint:
                            entry["endpoint"] = kt.publisher_endpoint
                        producer_topics.append({**entry, "topic": kt.topic_name})
                    if kt.role in ("consumer", "both"):
                        consumer_topics.append({**entry, "topic": kt.topic_name})
            else:
                # Fallback: use raw event strings
                for t in svc.events.produces:
                    producer_topics.append({"service": project, "topic": t})
                for t in svc.events.consumes:
                    consumer_topics.append({"service": project, "topic": t})

            for pt in producer_topics:
                topic = pt.pop("topic")
                flow.setdefault(topic, {"producers": [], "consumers": []})
                flow[topic]["producers"].append(pt)

            for ct in consumer_topics:
                topic = ct.pop("topic")
                flow.setdefault(topic, {"producers": [], "consumers": []})
                flow[topic]["consumers"].append(ct)

        return flow

    def _build_shared_models(self) -> list[str]:
        owners: dict[str, set[str]] = defaultdict(set)
        for svc in self.service_digests:
            for dto in svc.dtos:
                owners[dto].add(svc.project)
        shared = []
        for dto, projects in owners.items():
            if len(projects) > 1:
                shared.append(f"{dto} used in: {', '.join(sorted(projects))}")
        return sorted(shared)
