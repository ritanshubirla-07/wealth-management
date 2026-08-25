import re
from datetime import datetime
from .base import BaseParser, ParsedDocument, ParsedHolding
from .utils import parse_indian_number

class MFParser(BaseParser):
    def parse(self, text: str) -> ParsedDocument:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        
        client_name = "Unknown Client"
        statement_date = None
        
        # Simple heuristics for client and date
        for i, line in enumerate(lines):
            if 'Portfolio' in line or 'Statement' in line:
                if i > 0 and len(lines[i-1]) > 3 and not re.search(r'\d', lines[i-1]):
                    client_name = lines[i-1]
            date_match = re.search(r'(\d{2}[-/A-Za-z]{3,}[-/]\d{2,4})', line)
            if date_match and not statement_date:
                try:
                    # just extract a rough date string or skip for now if too hard to generalize
                    pass
                except ValueError:
                    pass

        doc = ParsedDocument(
            client_name=client_name,
            account_type='mutual_fund',
            statement_date=statement_date
        )
        
        current_category = 'Others'
        categories = ['Debt', 'Equity', 'Hybrid', 'Others', 'Liquid']
        
        i = 0
        while i < len(lines):
            line = lines[i]
            
            # Category detection
            for cat in categories:
                if line.upper() == cat.upper() or line.upper() == f"{cat.upper()} MUTUAL FUNDS":
                    current_category = cat
                    break
                    
            # Scheme detection - typically multiple words ending with 'Fund', 'Plan', 'Growth', 'Option'
            scheme_pattern = re.compile(r'(?i).*(Fund|Plan|Growth|Option|Dividend|ETF).*')
            
            if scheme_pattern.match(line) and i + 5 < len(lines):
                # Try to see if this is followed by Folio number and numeric data
                scheme_name = line
                folio = None
                
                j = 1
                if 'Folio' in lines[i+j]:
                    folio = lines[i+j].split()[-1] if len(lines[i+j].split()) > 1 else None
                    j += 1
                
                # Check for sequence of numbers
                vals = []
                while i + j < len(lines) and len(vals) < 8:
                    if re.search(r'[\d\.\,\-]', lines[i+j]):
                        # Could be a number
                        num_str = re.sub(r'[a-zA-Z]', '', lines[i+j]).strip()
                        if num_str:
                            vals.append(lines[i+j])
                    else:
                        break
                    j += 1
                
                # We need at least NAV, Units, Current Value, Invested Value
                if len(vals) >= 4:
                    try:
                        # Assuming common order: Invested Cost, Current Value, NAV, Units (or similar, heuristic fallback)
                        # We will extract whatever looks like a number and try to map if we had clear headers.
                        # For now, let's map based on a generic assumption of (Invested, Value, Gain, NAV, XIRR)
                        # To be safe, we might just grab the ones we know
                        
                        holding = ParsedHolding(
                            security_name=scheme_name,
                            folio_number=folio,
                            scheme_category=current_category,
                            asset_class='mutual_fund'
                        )
                        
                        # Let's map based on basic sorting of what they might be or just positional
                        # Actually, typical MF statement:
                        # Invested Cost, Current Value, Dividend, Unrealised, Abs Return, XIRR
                        
                        doc.holdings.append(holding)
                        i += j - 1
                    except Exception:
                        pass
            i += 1
            
        return doc
