import openpyxl
import pandas as pd
import json
import re

from .detector import ParsedDocument, populate_holdings


class ExcelParser:
    """Parser for spreadsheet statements.

    Header mapping and row filtering are delegated to
    :func:`detector.populate_holdings` so a fix made for PDFs applies here too —
    this module previously carried its own divergent copy of that logic.
    """

    def parse(self, file_path: str, file_name: str = "") -> ParsedDocument:
        wb = openpyxl.load_workbook(file_path, data_only=True)

        doc = ParsedDocument()
        doc.account_type = "excel"
        doc.portfolio_name = file_name or file_path
        doc.account_number = re.sub(r'\.xlsx?$', '', file_name or "", flags=re.IGNORECASE) or "Excel"

        all_rows = []
        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            for row in sheet.iter_rows(values_only=True):
                clean_row = [str(cell).strip() if cell is not None else "" for cell in row]
                if any(clean_row):
                    all_rows.append(clean_row)

        if not all_rows:
            return doc

        max_cols = max(len(row) for row in all_rows)
        df = pd.DataFrame([row + [""] * (max_cols - len(row)) for row in all_rows])

        # Detect header row
        header_idx = -1
        for idx, row in df.iterrows():
            row_str = " ".join([str(val).lower() for val in row])
            if "value" in row_str or "price" in row_str or "rate" in row_str:
                header_idx = idx
                break

        if header_idx != -1:
            headers = [str(h).strip().lower() if str(h).strip() else f"unnamed_{i}"
                       for i, h in enumerate(df.iloc[header_idx].values)]
            df.columns = headers
            df = df.iloc[header_idx + 1:].reset_index(drop=True)

        # Columns are only strings when a header row was found; a RangeIndex has no .str
        df.columns = [str(c) for c in df.columns]
        df = df.loc[:, ~df.columns.str.contains('^unnamed')]

        # Store RAW payloads for the LLM
        doc.raw_markdown = df.to_markdown(index=False)
        doc.raw_json = json.dumps(df.to_dict(orient='records'))

        populate_holdings(df, doc)
        return doc
