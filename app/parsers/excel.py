import json
import re

import openpyxl
import pandas as pd

from .detector import ParsedDocument, map_headers_deterministic, populate_holdings


class ExcelParser:
    """Parser for spreadsheet statements.

    Header mapping and row filtering are delegated to
    :func:`detector.populate_holdings` so a fix made for PDFs applies here too —
    this module previously carried its own divergent copy of that logic.

    Two things about real broker workbooks drive the shape of this parser:

    * **A sheet usually opens with a summary block.** "Invested Value 728019.74"
      sits above the table, and picking the first row that merely contains the
      word "value" lands on that summary instead of the real header. The header
      is found by scoring each candidate row against the schema instead.
    * **A workbook often holds several sheets that overlap.** An export with
      Equity, Mutual Funds and Combined tabs states most holdings twice, so
      concatenating every sheet double-counts the portfolio. Sheets are parsed
      separately and merged on identity.
    """

    def parse(self, file_path: str, file_name: str = "") -> ParsedDocument:
        workbook = openpyxl.load_workbook(file_path, data_only=True)

        doc = ParsedDocument()
        doc.account_type = "excel"
        doc.portfolio_name = file_name or file_path
        doc.account_number = re.sub(r'\.xlsx?$', '', file_name or "", flags=re.IGNORECASE) or "Excel"

        markdown_parts, json_parts = [], []
        for sheet_name in workbook.sheetnames:
            frame = self._sheet_frame(workbook[sheet_name])
            if frame is None:
                continue

            sheet_doc = ParsedDocument()
            populate_holdings(frame, sheet_doc)
            if not sheet_doc.holdings:
                continue

            self._merge(doc, sheet_doc.holdings)
            doc.has_cost_data = doc.has_cost_data or sheet_doc.has_cost_data
            markdown_parts.append(f"## {sheet_name}\n{frame.to_markdown(index=False)}")
            json_parts.append({"sheet": sheet_name, "rows": frame.to_dict(orient='records')})

        doc.raw_markdown = "\n\n".join(markdown_parts)
        doc.raw_json = json.dumps(json_parts, default=str)
        return doc

    # ── Sheet -> DataFrame ──────────────────────────────────────────────────

    def _sheet_frame(self, sheet):
        """One sheet as a DataFrame headed by its real header row, or None."""
        rows = []
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c).strip() if c is not None else "" for c in row]
            if any(cells):
                rows.append(cells)
        if len(rows) < 2:
            return None

        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]

        header_idx = self._find_header(rows)
        if header_idx is None:
            return None

        headers = [h.strip().lower() or f"unnamed_{i}"
                   for i, h in enumerate(rows[header_idx])]
        frame = pd.DataFrame(rows[header_idx + 1:], columns=headers)
        # Drop the padding columns and any the sheet left unlabelled
        frame = frame.loc[:, ~frame.columns.str.startswith('unnamed')]
        return frame if not frame.empty and len(frame.columns) else None

    @staticmethod
    def _find_header(rows) -> int:
        """Index of the row that best looks like a table header.

        Scored by how many schema columns its cells map to, so a two-cell
        summary line such as "Invested Value | 728019.74" cannot outrank the
        real header. A name or a value alone is not enough to win: the row has
        to look like a table, which is what the two-column floor enforces.
        """
        best_idx, best_score = None, 0
        for idx, row in enumerate(rows):
            labels = [c for c in row if c]
            if len(labels) < 2:
                continue
            mapped = map_headers_deterministic(labels)
            score = len(set(mapped.values()))
            # A header row names things; a data row carries numbers.
            numeric = sum(1 for c in labels if re.fullmatch(r'-?[\d,.]+%?', c))
            if numeric > len(labels) / 2:
                continue
            if score > best_score:
                best_idx, best_score = idx, score
        return best_idx if best_score >= 2 else None

    # ── Merging sheets ──────────────────────────────────────────────────────

    @staticmethod
    def _position_key(h) -> tuple:
        """What makes two rows the *same position* rather than the same security.

        Quantity and value are part of the identity on purpose. One sheet can
        legitimately list a security several times — the same stock held by two
        family members, or through both a demat account and a PMS — and those
        are separate positions that must both survive. Only a row restating the
        same security at the same size is a duplicate.
        """
        name = re.sub(r'[^A-Z0-9]', '', (h.security_name or "").upper())
        return (
            (h.isin or "").strip().upper() or name,
            round(h.quantity or 0, 3),
            round(h.current_value or 0, 2),
        )

    @classmethod
    def _merge(cls, doc: ParsedDocument, holdings: list) -> None:
        """Add a sheet's holdings, skipping ones an earlier sheet already stated.

        Only rows from *previous* sheets are treated as duplicates: a workbook
        with Equity, Mutual Funds and Combined tabs states most holdings twice,
        and counting the Combined tab as well would double the portfolio. Rows
        within the sheet being added are never collapsed against each other.
        """
        seen = {cls._position_key(h) for h in doc.holdings}
        for h in holdings:
            if cls._position_key(h) in seen:
                continue
            doc.holdings.append(h)
