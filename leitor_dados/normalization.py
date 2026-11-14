"""One normalization contract shared by importers and readers."""

from __future__ import annotations

import hashlib
import re


KINDS = ("cpf", "email", "phone", "code")
MAX_IDENTIFIER_LENGTH = 4096
DEFAULT_NORMALIZATION = {
    "cpf": {}, "email": {"lowercase": False}, "phone": {}, "code": {},
}


def normalization_settings(settings: dict | None = None) -> dict:
    """Validate settings and return an independent, explicit configuration."""
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("Configuração de normalização deve ser um objeto.")
    if set(settings) - set(KINDS) - {"email_lowercase"}:
        raise ValueError("Opção de normalização desconhecida.")
    result = {kind: dict(options) for kind, options in DEFAULT_NORMALIZATION.items()}
    for kind in KINDS:
        options = settings.get(kind, {})
        if not isinstance(options, dict):
            raise ValueError("Opções de normalização devem ser objetos.")
        allowed = {"lowercase"} if kind == "email" else set()
        if set(options) - allowed:
            raise ValueError("Opção de normalização desconhecida.")
        result[kind].update(options)
    if "email_lowercase" in settings:
        if "lowercase" in settings.get("email", {}):
            raise ValueError("Configure a opção de e-mail em apenas um local.")
        result["email"]["lowercase"] = settings["email_lowercase"]
    if not isinstance(result["email"]["lowercase"], bool):
        raise ValueError("A opção lowercase de e-mail deve ser booleana.")
    return result


def normalize(kind: str, value: str | None, settings: dict | None = None) -> str | None:
    """Normalize a textual identifier, preserving significant leading zeros.

    Missing/blank values return None. Validation checks representation only:
    it does not verify CPF check digits or prove that a phone/e-mail exists.
    Error messages deliberately do not contain the input value.
    """
    if kind not in KINDS:
        raise ValueError("Tipo de identificador desconhecido.")
    options = normalization_settings(settings)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Identificador deve ser texto; valores numéricos não são aceitos.")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError("Identificador excede o limite de tamanho.")
    value = value.strip()
    if not value:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Identificador contém caracteres de controle.")
    if kind == "cpf":
        if re.fullmatch(r"[0-9.\-\s]+", value) is None:
            raise ValueError("Formato de CPF inválido.")
        digits = re.sub(r"[^0-9]", "", value)
        if len(digits) != 11:
            raise ValueError("CPF deve conter onze dígitos ASCII.")
        return digits
    if kind == "phone":
        if re.fullmatch(r"\+?[0-9() .\-/\s]+", value) is None:
            raise ValueError("Formato de telefone inválido.")
        digits = re.sub(r"[^0-9]", "", value)
        if not 1 <= len(digits) <= 15:
            raise ValueError("Telefone deve conter entre um e quinze dígitos ASCII.")
        return digits
    if kind == "email":
        if value.count("@") != 1 or any(char.isspace() for char in value):
            raise ValueError("Formato de e-mail inválido.")
        local, domain = value.split("@")
        if not local or not domain:
            raise ValueError("Formato de e-mail inválido.")
        return f"{local.casefold() if options['email']['lowercase'] else local}@{domain.casefold()}"
    return value


def lookup_shard(kind: str, normalized_value: str, count: int) -> int:
    """Stable routing; unlike Python hash(), this is identical across processes."""
    if kind not in KINDS or not isinstance(normalized_value, str):
        raise ValueError("Identificador de roteamento inválido.")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("Quantidade de shards inválida.")
    key = (kind + "\0" + normalized_value).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big") % count


def data_shard(record_id: int, count: int) -> int:
    if (isinstance(record_id, bool) or not isinstance(record_id, int) or record_id < 1
            or isinstance(count, bool) or not isinstance(count, int) or count < 1):
        raise ValueError("Identificador ou quantidade de shards inválido.")
    return record_id % count
