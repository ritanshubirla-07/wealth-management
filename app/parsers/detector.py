import os
import fitz
from .base import ParsedDocument
from .demat import DematParser
from .pms import PMSParser
from .mutual_fund import MFParser
from .excel import ExcelParser

def detect_and_parse(file_path: str, file_name: str = "") -> ParsedDocument:
    ext = os.path.splitext(file_name or file_path)[1].lower()
    
    if ext in ['.xlsx', '.xls']:
        parser = ExcelParser()
        return parser.parse(file_path, file_name)
    elif ext == '.pdf':
        try:
            doc = fitz.open(file_path)
            text = ""
            for page in doc:
                text += page.get_text()
            doc.close()
            
            # Detect format
            if 'Demat Holding' in text or 'DP Id' in text or 'DP ID' in text:
                parser = DematParser()
                return parser.parse(text)
            elif 'Portfolio Appraisal' in text or 'By Asset Class' in text:
                parser = PMSParser()
                return parser.parse(text)
            elif 'XIRR' in text or 'NAV' in text or 'Folio' in text:
                parser = MFParser()
                return parser.parse(text)
            else:
                raise ValueError("Unknown PDF document format")
        except Exception as e:
            raise Exception(f"Failed to parse PDF: {str(e)}")
    else:
        raise ValueError(f"Unsupported file extension: {ext}")
