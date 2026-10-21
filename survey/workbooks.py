import io
import posixpath
import re
import zipfile
from copy import copy

import openpyxl
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

MAX_UPLOAD = 2 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
MAX_XML = 20 * 1024 * 1024
MAX_TEXT = 100_000
MAX_CELL_TEXT = 2000
MAX_BATCH_TEXT = 6000
HEADERS = {"content": "Translated Content", "messages": "Translated Messages"}
CELL_ADDRESS = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")


class InvalidWorkbook(ValueError):
    pass


def validate_archive(data):
    if len(data) > MAX_UPLOAD:
        raise InvalidWorkbook("The workbook must be no larger than 2 MiB.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > 200 or len(names) != len(set(names)):
                raise InvalidWorkbook("Workbook contains too many or duplicate archive entries.")
            if not {"[Content_Types].xml", "xl/workbook.xml"}.issubset(names):
                raise InvalidWorkbook("Upload a valid XLSX workbook.")
            total = 0
            cell_count = 0
            for entry in entries:
                name = entry.filename
                if (
                    name.startswith("/") or "\\" in name or ":" in name
                    or posixpath.normpath(name) != name.rstrip("/")
                    or ".." in name.split("/") or entry.flag_bits & 1
                ):
                    raise InvalidWorkbook("Unsafe workbook archive entry.")
                lower = name.lower()
                if any(part in lower for part in (
                    "vbaproject", "activex", "embeddings/", "externallinks/",
                    "connections.xml", "querytables/", "customui/", "macrosheets/",
                    "dialogsheets/",
                )):
                    raise InvalidWorkbook("Macros, embedded objects and data connections are not supported.")
                total += entry.file_size
                if total > MAX_XML or entry.file_size > max(1, entry.compress_size) * 200:
                    raise InvalidWorkbook("Workbook expands beyond the processing limits.")
                if lower.endswith((".xml", ".rels")):
                    content = archive.read(entry)
                    root = ElementTree.fromstring(content, forbid_dtd=True)
                    worksheet = lower.startswith("xl/worksheets/") and lower.endswith(".xml")
                    row_count = 0
                    for node in root.iter():
                        local_name = node.tag.rsplit("}", 1)[-1]
                        if worksheet and local_name == "row":
                            row_count += 1
                            row_number = node.attrib.get("r", "")
                            if not row_number.isdigit() or not 1 <= int(row_number) <= 5000 or row_count > 5000:
                                raise InvalidWorkbook("Workbook row dimensions exceed the survey limits.")
                        if worksheet and local_name == "c":
                            cell_count += 1
                            coordinate = CELL_ADDRESS.fullmatch(node.attrib.get("r", ""))
                            if (
                                coordinate is None or cell_count > 80_000
                                or int(coordinate[2]) > 5000
                                or openpyxl.utils.column_index_from_string(coordinate[1]) > 100
                            ):
                                raise InvalidWorkbook("Workbook cell dimensions exceed the survey limits.")
                        if local_name == "f":
                            raise InvalidWorkbook("Remove formulas from the source workbook before uploading.")
                        if node.attrib.get("TargetMode", "").casefold() == "external":
                            raise InvalidWorkbook("Remove external links from the workbook before uploading.")
                        content_type = node.attrib.get("ContentType", "").lower()
                        if any(value in content_type for value in ("macroenabled", "vba", "macrosheet", "dialogsheet")):
                            raise InvalidWorkbook("Macro-enabled workbooks are not supported.")
    except (zipfile.BadZipFile, DefusedXmlException, ElementTree.ParseError, RuntimeError) as exc:
        raise InvalidWorkbook("The workbook contains an invalid or unsafe archive/XML structure.") from exc


def extract_cells(data):
    validate_archive(data)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, keep_links=False)
    except (ValueError, KeyError, OSError) as exc:
        raise InvalidWorkbook("The workbook could not be read.") from exc
    cells = []
    columns = []
    found = set()
    total_cells = 0
    total_text = 0
    try:
        if len(wb.worksheets) > 20:
            raise InvalidWorkbook("Use no more than 20 worksheets.")
        for sheet in wb.worksheets:
            rows = sheet.max_row or 0
            cols = sheet.max_column or 0
            total_cells += rows * cols
            if rows > 5000 or cols > 100 or total_cells > 80_000:
                raise InvalidWorkbook("Workbook dimensions exceed the survey limits.")
            header = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
            source_columns = {}
            for index, value in enumerate(header, 1):
                name = str(value or "").strip().casefold()
                if name in HEADERS:
                    if name in source_columns:
                        raise InvalidWorkbook(f"Duplicate '{name}' header in '{sheet.title}'.")
                    source_columns[name] = index
                    found.add(name)
                    columns.append({"sheet": sheet.title, "source": index, "header": name})
            for row_index, row in enumerate(sheet.iter_rows(min_row=2), 2):
                for source_name, source_col in source_columns.items():
                    if source_col > len(row):
                        continue
                    cell = row[source_col - 1]
                    if cell.data_type in ("f", "e"):
                        raise InvalidWorkbook("Survey text cannot contain formulas or Excel errors.")
                    if cell.value is None or not str(cell.value).strip():
                        continue
                    text = str(cell.value)
                    if len(text) > MAX_CELL_TEXT:
                        raise InvalidWorkbook("Each survey cell must contain at most 2,000 characters.")
                    total_text += len(text)
                    if total_text > MAX_TEXT or len(cells) >= 2000:
                        raise InvalidWorkbook("A survey may contain at most 2,000 texts / 100,000 characters.")
                    cells.append({
                        "id": f"item{len(cells)}", "sheet": sheet.title,
                        "row": row_index, "source": source_col,
                        "header": source_name, "text": text,
                    })
    finally:
        wb.close()
    if found != set(HEADERS):
        raise InvalidWorkbook("The workbook must contain both Content and Messages column headers.")
    if not cells:
        raise InvalidWorkbook("No survey text was found.")
    return cells, columns


def make_batches(cells):
    batches = []
    batch = []
    length = 0
    for cell in cells:
        if batch and (len(batch) == 12 or length + len(cell["text"]) > MAX_BATCH_TEXT):
            batches.append(batch)
            batch = []
            length = 0
        batch.append(cell)
        length += len(cell["text"])
    if batch:
        batches.append(batch)
    return batches


def render_workbook(data, cells, columns, translations):
    if set(translations) != {cell["id"] for cell in cells}:
        raise InvalidWorkbook("Translation is incomplete.")
    wb = openpyxl.load_workbook(io.BytesIO(data), keep_links=False)
    try:
        destinations = {}
        for column in columns:
            sheet = wb[column["sheet"]]
            source = column["source"]
            heading = HEADERS[column["header"]]
            target = next(
                (col for col in range(source + 1, sheet.max_column + 1)
                 if str(sheet.cell(1, col).value or "").strip().casefold() == heading.casefold()),
                None,
            )
            if target is None:
                # Prefer the existing empty survey translation column.
                target = next(
                    (col for col in range(source + 1, sheet.max_column + 2)
                     if all(sheet.cell(row, col).value is None
                            for row in range(1, sheet.max_row + 1))),
                    sheet.max_column + 1,
                )
            destinations[(sheet.title, source)] = target
            sheet.cell(1, target, heading)._style = copy(sheet.cell(1, source)._style)
            source_letter = openpyxl.utils.get_column_letter(source)
            target_letter = openpyxl.utils.get_column_letter(target)
            sheet.column_dimensions[target_letter].width = sheet.column_dimensions[source_letter].width
        for cell in cells:
            sheet = wb[cell["sheet"]]
            target = sheet.cell(cell["row"], destinations[(sheet.title, cell["source"])])
            target.value = translations[cell["id"]]
            target.data_type = "s"  # Model output is always text, including strings starting with '='.
            target._style = copy(sheet.cell(cell["row"], cell["source"])._style)
        output = io.BytesIO()
        wb.save(output)
        if output.tell() > MAX_OUTPUT:
            raise InvalidWorkbook("The translated file exceeds the download-size limit.")
        return output.getvalue()
    finally:
        wb.close()
