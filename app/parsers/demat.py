import re
from datetime import datetime
from .base import BaseParser, ParsedDocument, ParsedHolding
from .utils import parse_indian_number


class DematParser(BaseParser):
    def parse(self, text: str) -> ParsedDocument:
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        client_name = "Unknown Client"
        dp_id = None
        account_number = None
        statement_date = None

        # Client name: typically the very first line (ALL CAPS name)
        # Or look for a pattern like "Name : XXXX"
        for i, line in enumerate(lines):
            # First all-caps multi-word line that looks like a name
            if i < 5 and re.match(r'^[A-Z][A-Z\s]+$', line) and len(line.split()) >= 2:
                client_name = line.strip()
                break

        text_oneline = " ".join(lines)

        # Extract DP ID and Account number - various formats
        dp_match = re.search(r'DP\s*I[Dd]\s*[:\-]?\s*([A-Z0-9]+)', text_oneline, re.IGNORECASE)
        if dp_match:
            dp_id = dp_match.group(1)

        acct_match = re.search(r'(?:Client|Account)\s*(?:Id|No|Number)\s*[:\-]?\s*(\d+)', text_oneline, re.IGNORECASE)
        if acct_match:
            account_number = acct_match.group(1)

        # If no account number found, try to extract from filename-style patterns
        if not account_number:
            # Look for 8-digit numbers near DP Id
            nums = re.findall(r'\b(\d{7,10})\b', text_oneline)
            if nums:
                account_number = nums[0]

        # Statement date
        for line in lines:
            if any(kw in line.lower() for kw in ['statement date', 'as on', 'as of', 'business date']):
                date_match = re.search(r'(\d{2}[-/]\d{2}[-/]\d{4})', line)
                if date_match:
                    try:
                        date_str = date_match.group(1).replace('-', '/')
                        statement_date = datetime.strptime(date_str, "%d/%m/%Y").date()
                    except ValueError:
                        pass

        doc = ParsedDocument(
            client_name=client_name,
            account_type='demat',
            dp_id=dp_id,
            account_number=account_number,
            custodian='HDFC_DEMAT',
            statement_date=statement_date,
        )

        # Extract holdings
        isin_pattern = re.compile(r'^INE[A-Z0-9]{9}$')

        i = 0
        while i < len(lines):
            line = lines[i]
            if isin_pattern.match(line):
                isin = line
                # Company name may span 1-3 lines, ends before scrip type
                name_parts = []
                j = i + 1
                while j < len(lines) and j < i + 4:
                    next_line = lines[j]
                    # Check if this line is a scrip type indicator
                    if re.match(r'^(EQ|BE|BL|BZ|SM|ST|XT)', next_line.split()[0] if next_line.split() else ''):
                        break
                    # Check if it's a number (quantity)
                    if re.match(r'^[\d,]+\.\d+$', next_line.replace(',', '')):
                        break
                    name_parts.append(next_line)
                    j += 1

                security_name = ' '.join(name_parts).strip()
                if not security_name:
                    i += 1
                    continue

                # Now at scrip type line
                scrip_type = lines[j] if j < len(lines) else 'EQ'

                # Find the next 3-4 numeric values: quantity, rate, value, status
                nums_found = []
                status = 'free'
                k = j + 1
                while k < len(lines) and k < j + 6:
                    val = lines[k]
                    if val.lower() in ('free', 'pledged'):
                        status = val.lower()
                        k += 1
                        break
                    if re.search(r'\d', val):
                        nums_found.append(val)
                    k += 1

                if len(nums_found) >= 3:
                    try:
                        quantity = parse_indian_number(nums_found[0])
                        current_price = parse_indian_number(nums_found[1])
                        current_value = parse_indian_number(nums_found[2])

                        holding = ParsedHolding(
                            security_name=security_name,
                            isin=isin,
                            quantity=quantity,
                            current_price=current_price,
                            current_value=current_value,
                            scrip_type=scrip_type.split()[0] if scrip_type else 'EQ',
                            status=status,
                            asset_class='equity',
                        )
                        doc.holdings.append(holding)
                        doc.total_value += current_value
                    except Exception:
                        pass
                i = k
                continue
            i += 1

        return doc
