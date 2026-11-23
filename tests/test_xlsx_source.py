from contextlib import closing
from datetime import datetime, time, timedelta
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from openpyxl import Workbook

from leitor_dados.consolidate import ConsolidationError, consolidate
from leitor_dados import xlsx_source


class ExcelConsolidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "LoteExcel"
        self.root.mkdir()
        self.source = self.root / "planilha.xlsx"
        self.final = self.root / "LoteExcel.sqlite"

    def workbook(self, rows, *, extra=None):
        workbook = Workbook()
        workbook.active.title = "Dados"
        for row in rows:
            workbook.active.append(row)
        if extra:
            extra(workbook)
        workbook.save(self.source)
        workbook.close()

    def patch_zip(self, name, change):
        replacement = self.root / "replacement.zip"
        with ZipFile(self.source) as source, ZipFile(replacement, "w", ZIP_DEFLATED) as target:
            for entry in source.infolist():
                content = source.read(entry.filename)
                if entry.filename == name:
                    content = change(content)
                target.writestr(entry, content)
        replacement.replace(self.source)

    def records(self):
        with closing(sqlite3.connect(self.final)) as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT dados FROM registros")]

    def test_real_xlsx_multiple_sheets_hidden_and_csv_dedup(self):
        def extra(workbook):
            sheet = workbook.create_sheet("Oculta")
            sheet.sheet_state = "hidden"
            sheet.append(["nome", "codigo"])
            sheet.append(["Pessoa ficticia", "001"])
            sheet.append(["Outra pessoa", "002"])
            workbook.create_sheet("Vazia")

        self.workbook([["codigo", "nome"], ["001", "Pessoa ficticia"]], extra=extra)
        (self.root / "a.csv").write_text("codigo,nome\n001,Pessoa ficticia\n", encoding="utf-8")
        backup = self.root / "backup"
        backup.mkdir()
        (backup / "original.xlsx").write_bytes(b"nao e necessario ler o backup")
        summary = consolidate(self.root, remove_sources=True, batch_size=1)
        self.assertEqual(summary["registros"], 2)
        self.assertEqual(summary["linhas_origem"], 4)
        self.assertEqual(set(path.name for path in self.root.iterdir()), {self.final.name, "backup"})

    def test_null_boolean_number_datetime_duration_and_error(self):
        self.workbook([
            ["texto", "inteiro", "real", "booleano", "vazio", "data", "hora", "duracao", "erro"],
            ["0001", 7, 2.5, True, None, datetime(2026, 1, 2, 12, 30), time(13, 14), timedelta(hours=27), "#DIV/0!"],
        ])
        consolidate(self.root, remove_sources=True)
        row = self.records()[0]
        self.assertEqual(row["texto"], "0001")
        self.assertEqual(row["inteiro"], 7)
        self.assertEqual(row["real"], 2.5)
        self.assertIs(row["booleano"], True)
        self.assertIsNone(row["vazio"])
        self.assertEqual(row["data"], {"$excel_datetime": "2026-01-02T12:30:00"})
        self.assertEqual(row["hora"], {"$excel_time": "13:14:00"})
        self.assertEqual(row["duracao"], {"$excel_duration_seconds": 97200.0})
        self.assertEqual(row["erro"], {"$excel_error": "#DIV/0!"})

    def test_formula_and_cached_value_are_both_saved_without_evaluation(self):
        self.workbook([["codigo", "total"], ["001", "=1+2"]])
        self.patch_zip("xl/worksheets/sheet1.xml", lambda value: re.sub(
            rb'(<f>1\+2</f>)<v(?:\s*/>|></v>)', rb'\1<v>3</v>', value))
        consolidate(self.root, remove_sources=True)
        self.assertEqual(self.records()[0]["total"], {"$excel_formula": "=1+2", "cached": 3})

    def test_formula_without_cached_value_keeps_expression(self):
        self.workbook([["total"], ["=1+2"]])
        consolidate(self.root, remove_sources=True)
        self.assertEqual(self.records(), [{"total": {"$excel_formula": "=1+2", "cached": None}}])

    def test_wrong_dimension_does_not_truncate_rows_or_columns(self):
        self.workbook([["codigo", "nome"], ["001", "Exemplo A"], ["002", "Exemplo B"]])
        self.patch_zip("xl/worksheets/sheet1.xml", lambda value: re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', value))
        self.assertEqual(consolidate(self.root, remove_sources=True)["registros"], 2)

    def test_sparse_rows_blank_rows_and_short_rows(self):
        self.workbook([[None], ["codigo", "nome", None], ["001", "Exemplo"], [], ["002"]])
        consolidate(self.root, remove_sources=True)
        self.assertIn({"codigo": "002", "nome": None}, self.records())
        self.assertEqual(len(self.records()), 2)

    def test_data_outside_header_blocks_cleanup(self):
        self.workbook([["codigo"], ["001"], ["002", "campo sem nome"]])
        with self.assertRaisesRegex(ConsolidationError, "além do cabeçalho"):
            consolidate(self.root, remove_sources=True, batch_size=1)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.final.exists())

    def test_duplicate_or_nontext_headers_block_cleanup(self):
        for header in (["id", "id"], ["id", None, "nome"], [1, "nome"], ["=1+2", "nome"]):
            with self.subTest(header=header):
                self.workbook([header, ["001", "Exemplo"]])
                pending = self.root / ".LoteExcel.importando.sqlite"
                if pending.exists():
                    pending.unlink()
                with self.assertRaises(ConsolidationError):
                    consolidate(self.root, remove_sources=True)
                self.assertTrue(self.source.exists())

    def test_corrupt_xlsx_blocks_cleanup_of_other_sources(self):
        self.source.write_bytes(b"arquivo invalido")
        text = self.root / "a.txt"
        text.write_text("registro ficticio")
        with self.assertRaisesRegex(ConsolidationError, "XLSX inválida"):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(text.exists())
        self.assertTrue(self.source.exists())
        self.assertFalse(self.final.exists())

    def test_missing_dependency_blocks_before_database_creation(self):
        self.workbook([["codigo"], ["001"]])
        with patch.dict("sys.modules", {"defusedxml": None}):
            with self.assertRaisesRegex(ConsolidationError, "requirements-excel"):
                consolidate(self.root, remove_sources=True)
        self.assertTrue(self.source.exists())
        self.assertFalse((self.root / ".LoteExcel.importando.sqlite").exists())

    def test_disabled_xml_protection_blocks_before_database_creation(self):
        self.workbook([["codigo"], ["001"]])
        with patch("openpyxl.xml.functions.DEFUSEDXML", False):
            with self.assertRaisesRegex(ConsolidationError, "proteção XML"):
                consolidate(self.root, remove_sources=True)
        self.assertFalse((self.root / ".LoteExcel.importando.sqlite").exists())

    def test_xlsx_retry_keeps_completed_file_and_deduplication(self):
        self.workbook([["codigo"], ["001"], ["002"]])

        def interrupt(event):
            if event["stage"] == "rows_committed":
                raise ConsolidationError("interrompido")

        with self.assertRaises(ConsolidationError):
            consolidate(self.root, remove_sources=True, batch_size=1, progress=interrupt)
        self.assertTrue(self.source.exists())
        self.assertEqual(consolidate(self.root, remove_sources=True, batch_size=1)["registros"], 2)
        self.assertEqual(list(self.root.iterdir()), [self.final])

    def test_excel_headers_limit_blocks_instead_of_truncating(self):
        self.workbook([[f"col{i}" for i in range(257)], ["valor"] * 257])
        with self.assertRaisesRegex(ConsolidationError, "256"):
            consolidate(self.root, remove_sources=True)
        self.assertTrue(self.source.exists())

    def test_unhandled_sheet_xml_error_is_reported_without_cell_contents(self):
        self.workbook([["codigo"], ["001"]])
        self.patch_zip("xl/worksheets/sheet1.xml", lambda value: b"XML invalido com conteudo privado ficticio")
        with self.assertRaises(ConsolidationError) as result:
            consolidate(self.root, remove_sources=True)
        self.assertNotIn("conteudo privado", str(result.exception))
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
