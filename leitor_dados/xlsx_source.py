"""Optional XLSX reader. Never evaluates formulas or modifies the workbook."""
from contextlib import ExitStack, closing
from datetime import date, datetime, time, timedelta
from itertools import zip_longest

from .consolidate import ConsolidationError, _header


def check_support():
    try:
        import defusedxml  # noqa: F401 -- required before accepting any Excel source
        from openpyxl import load_workbook
        from openpyxl.xml.functions import DEFUSEDXML
    except ImportError:
        raise ConsolidationError(
            "Para ler .xlsx, instale requirements-excel.txt no ambiente virtual do projeto "
            "e execute com o Python desse ambiente. Nenhuma fonte foi apagada.") from None
    if not DEFUSEDXML:
        raise ConsolidationError("A proteção XML do leitor Excel está desativada; confira OPENPYXL_DEFUSEDXML.")
    return load_workbook


def _value(cell):
    if cell is None:
        return None
    value = cell.value
    if cell.data_type == "e":
        return {"$excel_error": value}
    if isinstance(value, datetime):
        return {"$excel_datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$excel_date": value.isoformat()}
    if isinstance(value, time):
        return {"$excel_time": value.isoformat()}
    if isinstance(value, timedelta):
        return {"$excel_duration_seconds": value.total_seconds()}
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ConsolidationError("Tipo de célula Excel não suportado; exporte essa aba para CSV.")
    return value


def iter_records(path):
    load_workbook = check_support()
    try:
        with ExitStack() as stack:
            # Own the file handles even if load_workbook fails before returning.
            source_stream = stack.enter_context(path.open("rb"))
            cached_stream = stack.enter_context(path.open("rb"))
            workbook = stack.enter_context(closing(load_workbook(source_stream, read_only=True, data_only=False, keep_links=False)))
            cached = stack.enter_context(closing(load_workbook(cached_stream, read_only=True, data_only=True, keep_links=False)))
            for sheet in workbook.worksheets:
                saved = cached[sheet.title]
                # Some exporters incorrectly declare A1:A1 despite containing more data.
                sheet.reset_dimensions()
                saved.reset_dimensions()
                columns = None
                with closing(sheet.iter_rows()) as rows, closing(saved.iter_rows()) as values:
                    for number, pair in enumerate(zip_longest(rows, values), start=1):
                        row, saved_row = pair
                        if row is None or saved_row is None or len(row) != len(saved_row):
                            raise ConsolidationError("As leituras da planilha e do cache não coincidem.")
                        if not any(cell.value is not None for cell in row):
                            continue
                        if columns is None:
                            headers = list(row)
                            while headers and headers[-1].value is None:
                                headers.pop()
                            if any(not isinstance(cell.value, str) or cell.data_type in {"f", "e"} for cell in headers):
                                raise ConsolidationError(f"Cabeçalho inválido na aba {sheet.title!r}, linha {number}; use nomes de colunas em texto.")
                            columns = _header([cell.value for cell in headers])
                            continue
                        if any(cell.value is not None for cell in row[len(columns):]):
                            raise ConsolidationError(f"Há dados além do cabeçalho na aba {sheet.title!r}, linha {number}.")
                        record = {}
                        for index, column in enumerate(columns):
                            cell = row[index] if index < len(row) else None
                            if cell is not None and cell.data_type == "f":
                                if not isinstance(cell.value, str):
                                    raise ConsolidationError("Fórmula matricial/especial não suportada; exporte para CSV.")
                                record[column] = {"$excel_formula": cell.value, "cached": _value(saved_row[index])}
                            else:
                                record[column] = _value(cell)
                        yield sheet.title, record
    except ConsolidationError:
        raise
    except Exception as error:
        # Library exceptions may embed source cell contents: report only the type.
        raise ConsolidationError(f"Planilha XLSX inválida ou ilegível ({type(error).__name__}); confira o arquivo ou exporte para CSV.") from None
