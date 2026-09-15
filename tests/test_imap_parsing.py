from __future__ import annotations

from programador.imap_client import parse_fetch_response, quote_folder


def test_quote_folder_encodes_and_quotes():
    assert quote_folder("INBOX") == '"INBOX"'
    assert quote_folder("Sent Messages") == '"Sent Messages"'
    assert quote_folder("Facturación") == '"Facturaci&APM-n"'


def test_quote_folder_escapes_injection_characters():
    # Un nombre con comillas no puede cerrar el literal y colar otro comando.
    assert quote_folder('mal"nombre') == '"mal\\"nombre"'
    assert quote_folder("back\\slash") == '"back\\\\slash"'


def test_parse_fetch_response_extracts_uid_flags_and_body():
    data = [
        (b"1 (UID 42 FLAGS (\\Seen \\Flagged) BODY[] {12}", b"Hola mundo!!"),
        b")",
        (b"2 (UID 43 FLAGS () BODY[] {5}", b"otro."),
        b")",
    ]
    parsed = parse_fetch_response(data)
    assert [(uid, flags) for uid, flags, _ in parsed] == [
        (42, ["\\Seen", "\\Flagged"]),
        (43, []),
    ]
    assert parsed[0][2] == b"Hola mundo!!"


def test_parse_fetch_response_ignores_noise_and_missing_uid():
    data = [b")", None, (b"1 (FLAGS (\\Seen) BODY[] {3}", b"abc"), ("no-bytes", "tampoco")]
    assert parse_fetch_response(data) == []
