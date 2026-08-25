import openpyxl
import re
from typing import Optional
from datetime import datetime
from .base import ParsedDocument, ParsedHolding
from .utils import parse_indian_number

class ExcelParser:
    def parse(self, file_path: str, file_name: str = "") -> ParsedDocument:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        
        # We can extract client name from filename if possible
        client_name = "Unknown Client"
        clean_filename = re.sub(r'\.xlsx?$', '', file_name, flags=re.IGNORECASE)
        if clean_filename:
            client_name = clean_filename
            
        doc = ParsedDocument(
            client_name=client_name,
            account_type='excel',
        )
        
        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            
            # Find header row
            header_row = -1
            col_map = {}
            
            keywords = {
                'security': ['security', 'stock', 'scheme', 'scrip', 'company', 'fund', 'name'],
                'isin': ['isin'],
                'quantity': ['qty', 'quantity', 'units', 'shares', 'balance'],
                'price': ['price', 'rate', 'nav', 'cmp', 'ltp'],
                'value': ['value', 'amount', 'current value', 'market value', 'mkt value'],
                'cost': ['cost', 'invested', 'investment', 'avg cost', 'purchase'],
                'gain': ['gain', 'profit', 'p&l', 'pnl', 'return'],
                'sector': ['sector', 'industry']
            }
            
            for row_idx, row in enumerate(sheet.iter_rows(values_only=True)):
                if header_row == -1:
                    # Check if this row looks like a header
                    matches = 0
                    temp_map = {}
                    for col_idx, cell_value in enumerate(row):
                        if not isinstance(cell_value, str):
                            continue
                        val_lower = str(cell_value).lower().strip()
                        for key, kws in keywords.items():
                            if any(kw in val_lower for kw in kws):
                                temp_map[key] = col_idx
                                matches += 1
                                break
                    
                    if matches >= 3: # Found a good header row
                        header_row = row_idx
                        col_map = temp_map
                elif header_row != -1:
                    # Parse data rows
                    if 'security' not in col_map:
                        break # Cannot proceed without security name
                        
                    sec_name = row[col_map['security']]
                    if not sec_name:
                        continue
                        
                    isin = row[col_map['isin']] if 'isin' in col_map else None
                    qty = row[col_map['quantity']] if 'quantity' in col_map else None
                    price = row[col_map['price']] if 'price' in col_map else None
                    value = row[col_map['value']] if 'value' in col_map else None
                    cost = row[col_map['cost']] if 'cost' in col_map else None
                    gain = row[col_map['gain']] if 'gain' in col_map else None
                    sector = row[col_map['sector']] if 'sector' in col_map else None
                    
                    # Clean up numbers
                    def to_float(val) -> Optional[float]:
                        if val is None:
                            return None
                        if isinstance(val, (int, float)):
                            return float(val)
                        return parse_indian_number(str(val))
                        
                    qty_f = to_float(qty)
                    price_f = to_float(price)
                    value_f = to_float(value) or 0.0
                    cost_f = to_float(cost)
                    gain_f = to_float(gain)
                    
                    holding = ParsedHolding(
                        security_name=str(sec_name).strip(),
                        isin=str(isin).strip() if isin else None,
                        quantity=qty_f,
                        current_price=price_f,
                        current_value=value_f,
                        total_cost=cost_f,
                        unrealized_gain=gain_f,
                        sector=str(sector).strip() if sector else None,
                        asset_class='equity' # Default
                    )
                    
                    doc.holdings.append(holding)
                    doc.total_value += value_f
                    
        return doc
