import csv
import io

MAX_GLOSSARY = 256 * 1024


class InvalidGlossary(ValueError):
    pass


def parse_glossary(text, source, targets):
    if not isinstance(text, str) or not text.strip():
        raise InvalidGlossary("Choose a non-empty UTF-8 glossary CSV.")
    if len(text.encode("utf-8")) > MAX_GLOSSARY:
        raise InvalidGlossary("The glossary must be no larger than 256 KiB.")
    text = text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,\t")
        reader = csv.reader(io.StringIO(text, newline=""), dialect, strict=True)
        headers = [value.strip() for value in next(reader)]
        if not all(headers) or len(headers) != len(set(headers)):
            raise InvalidGlossary("CSV column headers must be non-empty and unique.")
        if source not in headers or not set(targets).intersection(headers):
            raise InvalidGlossary(f"CSV headers must include {source} and at least one target language code.")
        rows = []
        for line, values in enumerate(reader, 2):
            if not values or not any(value.strip() for value in values):
                continue
            if len(values) != len(headers):
                raise InvalidGlossary(f"CSV row {line} has a different number of columns than the header.")
            if any(len(value) > 2000 or "\x00" in value for value in values):
                raise InvalidGlossary(f"CSV row {line} contains an invalid or oversized term.")
            rows.append(dict(zip(headers, (value.strip() for value in values))))
            if len(rows) > 5000:
                raise InvalidGlossary("The glossary must contain at most 5,000 rows.")
        if not any(row[source] and any(row.get(target) for target in targets) for row in rows):
            raise InvalidGlossary("The CSV must contain at least one source term and its translation.")
        return headers, rows
    except (csv.Error, StopIteration, IndexError) as exc:
        raise InvalidGlossary("Use a valid CSV separated by semicolons, commas or tabs.") from exc
