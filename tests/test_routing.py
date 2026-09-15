from __future__ import annotations

import pytest

from conftest import make_message
from programador.config import ConfigError, parse_routing_config


def test_default_entity_when_nothing_matches(router):
    result = router.route(make_message())
    assert result.entity == "personal"
    assert result.reason == "default"


def test_alias_recipient_wins_over_lower_priority_rule(router):
    # El correo cumple la regla de asunto de FOREGO (prioridad 60) y la de alias
    # de Rambaid (prioridad 100): debe ganar el alias.
    message = make_message(
        to_addrs=["rambaid@lester.es"], subject="Pedido para FOREGO INTL"
    )
    result = router.route(message)
    assert result.entity == "rambaid"
    assert result.reason == "to_addr=rambaid@lester.es"
    assert result.rule_priority == 100


def test_from_domain_match_is_exact_not_suffix(router):
    legit = make_message(from_addr="avisos@agenciatributaria.gob.es")
    assert router.route(legit).entity == "rambaid"

    # Un dominio de phishing que solo TERMINA igual no debe colarse como Hacienda.
    spoofed = make_message(from_addr="avisos@fake-agenciatributaria.gob.es.attacker.com")
    assert router.route(spoofed).entity == "personal"


def test_subject_and_body_criteria(router):
    assert router.route(make_message(subject="Re: FOREGO Intl shipment")).entity == "forego_intl"
    assert router.route(make_message(body_text="Emitido por S@ND SRL")).entity == "sand"


def test_routing_is_accent_and_case_insensitive(router):
    message = make_message(to_addrs=["RAMBAID@Lester.ES"])
    assert router.route(message).entity == "rambaid"


def test_doc_types_from_subject_body_and_attachment_names(router):
    from programador.models import Attachment

    message = make_message(
        subject="Envío de documentación",
        body_text="Adjunto la factura correspondiente",
        attachments=[Attachment(filename="BL-MSCU123 bill of lading.pdf", content_type="application/pdf", size=10)],
    )
    assert router.doc_types(message) == ["bill_of_lading", "invoice"]


def test_apply_sets_all_fields(router):
    message = router.apply(make_message(subject="Aduana - despacho", to_addrs=["rambaid@lester.es"]))
    assert message.entity == "rambaid"
    assert message.routing_reason.startswith("to_addr=")
    assert message.doc_types == ["customs"]


def test_config_rejects_rule_pointing_at_unknown_entity():
    with pytest.raises(ConfigError, match="entidad desconocida"):
        parse_routing_config(
            {
                "entities": [{"id": "rambaid", "name": "Rambaid"}],
                "default_entity": "rambaid",
                "routing_rules": [{"entity": "typo_entity", "match": {}}],
            }
        )


def test_config_rejects_unknown_default_entity():
    with pytest.raises(ConfigError, match="default_entity"):
        parse_routing_config(
            {"entities": [{"id": "rambaid", "name": "Rambaid"}], "default_entity": "personal"}
        )
