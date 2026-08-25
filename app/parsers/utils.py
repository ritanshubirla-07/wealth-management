import re

def parse_indian_number(num_str: str) -> float:
    if not num_str or not num_str.strip():
        return 0.0
    num_str = num_str.strip()
    
    # Handle negative numbers with parentheses or minus sign
    is_negative = False
    if num_str.startswith('(') and num_str.endswith(')'):
        is_negative = True
        num_str = num_str[1:-1]
    elif num_str.startswith('-'):
        is_negative = True
        num_str = num_str[1:]
        
    # Remove commas and spaces
    num_str = re.sub(r'[,\s]', '', num_str)
    
    # Handle percentage signs
    if num_str.endswith('%'):
        num_str = num_str[:-1]
        
    try:
        val = float(num_str)
        return -val if is_negative else val
    except ValueError:
        return 0.0
