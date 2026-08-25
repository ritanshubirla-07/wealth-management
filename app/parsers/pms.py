import re
from datetime import datetime
from .base import BaseParser, ParsedDocument, ParsedHolding
from .utils import parse_indian_number

class PMSParser(BaseParser):
    def parse(self, text: str) -> ParsedDocument:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        
        client_name = "Unknown Client"
        account_number = None
        portfolio_name = None
        statement_date = None
        
        for i, line in enumerate(lines):
            if line.startswith('Account :'):
                parts = line.split('Account :')[1].strip().split()
                if parts:
                    account_number = parts[0]
                    client_name = " ".join(parts[1:])
                if i + 1 < len(lines):
                    portfolio_name = lines[i+1]
            if 'As of' in line:
                date_match = re.search(r'As of\s+(\d{2}/\d{2}/\d{4})', line)
                if date_match:
                    try:
                        statement_date = datetime.strptime(date_match.group(1), "%d/%m/%Y").date()
                    except ValueError:
                        pass
        
        doc = ParsedDocument(
            client_name=client_name,
            account_type='pms',
            account_number=account_number,
            portfolio_name=portfolio_name,
            statement_date=statement_date
        )
        
        # Security name pattern: ALL CAPS ending in LTD or LIMITED, at least 2 words
        sec_pattern = re.compile(r'^([A-Z0-9\s\.\&\-]+)\s+(LTD\.?|LIMITED)$', re.IGNORECASE)
        
        i = 0
        while i < len(lines):
            line = lines[i]
            
            # Additional check: all caps for security names to avoid false positives
            match_line = line
            consumed_lines = 1
            if not sec_pattern.match(match_line) and i + 1 < len(lines):
                # Try combining with next line for multi-line names (e.g. ELECON ENGINEERING COMPANY \n LTD)
                combined = line + " " + lines[i+1]
                if sec_pattern.match(combined) and combined.isupper():
                    match_line = combined
                    consumed_lines = 2
            
            if sec_pattern.match(match_line) and match_line.isupper() and len(match_line.split()) >= 2:
                security_name = match_line
                
                try:
                    vals = []
                    j = consumed_lines
                    while i + j < len(lines) and len(vals) < 9:
                        next_line = lines[i+j]
                        if re.search(r'\d', next_line) and not next_line.isupper() and not next_line.isalpha():
                            vals.append(next_line)
                        else:
                            break
                        j += 1

                        
                    if len(vals) >= 8:
                        price = parse_indian_number(vals[0])
                        gain_loss = parse_indian_number(vals[1])
                        qty = parse_indian_number(vals[2])
                        unit_cost = parse_indian_number(vals[3])
                        total_cost = parse_indian_number(vals[4])
                        mkt_value = parse_indian_number(vals[5])
                        pct_gain = parse_indian_number(vals[6])
                        pct_assets = parse_indian_number(vals[7])
                        
                        # Check if accrued income was parsed as the 9th value
                        accrued = None
                        if len(vals) == 9:
                            accrued = parse_indian_number(vals[8])
                        
                        holding = ParsedHolding(
                            security_name=security_name,
                            quantity=qty,
                            avg_cost=unit_cost,
                            total_cost=total_cost,
                            current_price=price,
                            current_value=mkt_value,
                            accrued_income=accrued,
                            unrealized_gain=gain_loss,
                            gain_pct=pct_gain,
                            weight_pct=pct_assets,
                            asset_class='equity'
                        )
                        doc.holdings.append(holding)
                        doc.total_value += mkt_value
                        
                        i += j - 1
                except Exception:
                    pass
            i += 1
            
        return doc
