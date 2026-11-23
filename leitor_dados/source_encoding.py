"""Strict per-file text decoding policy; auto is a documented convention, not certainty."""
import codecs

from .consolidate import ConsolidationError, _fingerprint, _json, _signature


TEXT_SUFFIXES = {".csv", ".tsv", ".txt"}


def _validate(path, encoding):
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            text = decoder.decode(chunk, final=False)
            if "\x00" in text:
                raise ConsolidationError("Texto contém NUL; confira se é binário ou UTF-16/32 sem BOM. Defina a codificação explicitamente.")
        if "\x00" in decoder.decode(b"", final=True):
            raise ConsolidationError("Texto contém NUL; codificação precisa ser definida explicitamente.")


def detect(path):
    before = _signature(path)
    with path.open("rb") as stream:
        prefix = stream.read(4)
    candidates = ["utf-8-sig", "cp1252"]
    for marker, encoding in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                             (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
                             (codecs.BOM_UTF8, "utf-8-sig")):
        if prefix.startswith(marker):
            candidates = [encoding]
            break
    for encoding in candidates:
        try:
            _validate(path, encoding)
        except UnicodeDecodeError:
            continue
        if _signature(path) != before:
            raise ConsolidationError("A fonte mudou durante a conferência de codificação.")
        return encoding
    raise ConsolidationError("Codificação não reconhecida pela política auto (BOM, UTF-8, CP1252). Fontes preservadas; defina a codificação correta.")


def upgrade_to_auto(connection, root, options, progress):
    """Upgrade only the previous UTF-8 policy on a provisional, fully available input tree.

    Previously completed sources retain their known decoder. If an interrupted
    source has committed rows under a different decoder, remove only records
    first introduced by that source and replay every source to restore any shared
    occurrences. Counts and ownership remain consistent with the final union.
    """
    import json

    saved_options = json.loads(connection.execute("SELECT valor FROM configuracao WHERE chave='opcoes'").fetchone()[0])
    if saved_options == options:
        return
    sources = list(connection.execute("SELECT id,caminho,assinatura,sha256,completo,novos,encoding FROM fontes"))
    selected, reset = {}, []
    for source_id, name, signature, checksum, complete, new_count, previous in sources:
        path = root / name
        if path.suffix.casefold() not in TEXT_SUFFIXES:
            continue
        previous = previous or saved_options["encoding"]
        if progress and not complete:
            progress({"stage": "checking_encoding", "arquivo": name})
        encoding = previous if complete else detect(path)
        selected[source_id] = encoding
        if encoding != previous:
            if _fingerprint(path) != (json.loads(signature), checksum):
                raise ConsolidationError("Origem mudou desde a tentativa anterior; migração de codificação cancelada.")
            reset.append((source_id, new_count))
    replay = any(count for _, count in reset)
    if replay:
        # All original records must still be reproducible before removing any
        # provisional records, even if a later source shared their previous hash.
        for _, name, signature, checksum, _, _, _ in sources:
            if progress:
                progress({"stage": "checking_source", "arquivo": name})
            if _fingerprint(root / name) != (json.loads(signature), checksum):
                raise ConsolidationError("Uma fonte mudou; o banco provisório não foi reinterpretado.")
    if progress and replay:
        progress({"stage": "rebuilding_partial_sources", "count": len(reset)})
    connection.execute("BEGIN IMMEDIATE")
    try:
        for source_id, count in reset:
            if count:
                connection.execute("DELETE FROM registros WHERE fonte_id=?", (source_id,))
            connection.execute("UPDATE fontes SET completo=0, linhas=0, novos=0 WHERE id=?", (source_id,))
        if replay:
            connection.execute("UPDATE fontes SET completo=0")
        connection.executemany("UPDATE fontes SET encoding=? WHERE id=?", [(encoding, source_id) for source_id, encoding in selected.items()])
        connection.execute("UPDATE configuracao SET valor=? WHERE chave='opcoes'", (_json(options),))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    if progress:
        progress({"stage": "encoding_policy_upgraded", "encoding": "auto", "fontes_reinterpretadas": len(reset),
                  "reler_fontes": replay})
