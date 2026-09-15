"""Motor de enrutado por entidad y de pistas de tipo documental.

Deliberadamente determinista: reglas declarativas evaluadas por prioridad.
Una decision de enrutado siempre puede explicarse ("caso to_addr=..."), lo que
importa cuando el correo que se clasifica mal es una factura de aduanas.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import RoutingConfig
from .models import Message, RoutingRule
from .text import normalize

__all__ = ["RoutingResult", "Router"]


@dataclass(slots=True)
class RoutingResult:
    entity: str
    reason: str
    rule_priority: int | None = None


def _domain_of(address: str) -> str:
    _, _, domain = normalize(address).partition("@")
    return domain


class Router:
    def __init__(self, config: RoutingConfig) -> None:
        self._config = config
        # Prioridad descendente; `sorted` es estable, asi que dentro de la misma
        # prioridad gana la regla escrita antes en el YAML.
        self._rules: list[RoutingRule] = sorted(
            config.rules, key=lambda r: r.priority, reverse=True
        )
        self._hints: dict[str, list[str]] = {
            doc_type: [normalize(k) for k in keywords if normalize(k)]
            for doc_type, keywords in config.doc_type_hints.items()
        }

    @property
    def config(self) -> RoutingConfig:
        return self._config

    def route(self, message: Message) -> RoutingResult:
        """Devuelve la entidad del mensaje y el motivo de la decision."""
        for rule in self._rules:
            matched = self._match(rule, message)
            if matched is not None:
                return RoutingResult(
                    entity=rule.entity, reason=matched, rule_priority=rule.priority
                )
        return RoutingResult(entity=self._config.default_entity, reason="default")

    def _match(self, rule: RoutingRule, message: Message) -> str | None:
        from_addr_n = normalize(message.from_addr)
        from_domain_n = _domain_of(message.from_addr)
        recipients = {normalize(a) for a in (*message.to_addrs, *message.cc_addrs)}

        for domain in rule.from_domain:
            if from_domain_n and from_domain_n == normalize(domain).lstrip("@"):
                return f"from_domain={normalize(domain)}"
        for addr in rule.from_addr:
            if from_addr_n and from_addr_n == normalize(addr):
                return f"from_addr={normalize(addr)}"
        for addr in rule.to_addr:
            if normalize(addr) in recipients:
                return f"to_addr={normalize(addr)}"

        subject_n = normalize(message.subject)
        for needle in rule.subject_contains:
            needle_n = normalize(needle)
            if needle_n and needle_n in subject_n:
                return f"subject_contains={needle_n}"

        body_n = normalize(message.body_text)
        for needle in rule.body_contains:
            needle_n = normalize(needle)
            if needle_n and needle_n in body_n:
                return f"body_contains={needle_n}"

        return None

    def doc_types(self, message: Message) -> list[str]:
        """Tipos documentales sugeridos, ordenados alfabeticamente.

        Mira asunto, cuerpo y nombres de adjuntos: en COMEX el nombre del
        fichero ("BL-MSCU1234567.pdf") suele decir mas que el cuerpo.
        """
        haystack = " ".join(
            (
                normalize(message.subject),
                normalize(message.body_text),
                normalize(" ".join(a.filename for a in message.attachments)),
            )
        )
        found = {
            doc_type
            for doc_type, keywords in self._hints.items()
            if any(keyword in haystack for keyword in keywords)
        }
        return sorted(found)

    def apply(self, message: Message) -> Message:
        """Enruta y etiqueta el mensaje in situ, y lo devuelve."""
        result = self.route(message)
        message.entity = result.entity
        message.routing_reason = result.reason
        message.doc_types = self.doc_types(message)
        return message
