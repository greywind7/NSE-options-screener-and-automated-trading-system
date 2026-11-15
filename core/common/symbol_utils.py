"""
Canonical Symbol & Instrument Token Sanitization Utilities.
Standardizes internal representation to EXCHANGE:SYMBOL across all data, scoring, and execution tiers.
Resolves Gap 7 (33 duplicate .replace() mutations on critical paths).
"""
import re
from typing import Optional, Tuple

# Pre-compiled regex patterns for speed (< 1 us)
# Option contracts must have strike price (digits) immediately before CE or PE (e.g. RELIANCE24NOV2500CE)
_OPTION_REGEX = re.compile(r"^[A-Z0-9_\-\|:]*?[A-Z0-9]+?\d+(?:\.\d+)?(?:CE|PE)$", re.IGNORECASE)
_INDEX_NAMES = {"NIFTY 50", "NIFTY BANK", "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}


def normalize_instrument_token(token: str) -> str:
    """
    Standardize any token representation to canonical format.
    Examples:
        'NSE_EQ|RELIANCE' -> 'NSE:RELIANCE'
        'NSE_FO|TCS26NOV3500CE' -> 'NSE:TCS26NOV3500CE'
        'NSE_INDEX|Nifty 50' -> 'NSE_INDEX:Nifty 50'
        'NSE:RELIANCE' -> 'NSE:RELIANCE'
        'MCX:CRUDEOIL24DEC' -> 'MCX:CRUDEOIL24DEC'
    """
    if not token:
        return ""
    
    token = token.strip()
    
    # Fast path if already standard colon format without legacy underscores in prefix
    if ":" in token and "|" not in token:
        parts = token.split(":", 1)
        prefix, symbol = parts[0], parts[1]
        if prefix in ("NSE_EQ", "NSE_FO"):
            return f"NSE:{symbol}"
        elif prefix in ("BSE_EQ", "BSE_FO"):
            return f"BSE:{symbol}"
        elif prefix in ("MCX_FO",):
            return f"MCX:{symbol}"
        return token

    # Handle pipe separator
    if "|" in token:
        parts = token.split("|", 1)
        prefix, symbol = parts[0], parts[1]
        if prefix in ("NSE_EQ", "NSE_FO"):
            prefix = "NSE"
        elif prefix in ("BSE_EQ", "BSE_FO"):
            prefix = "BSE"
        elif prefix in ("MCX_FO",):
            prefix = "MCX"
        return f"{prefix}:{symbol}"

    return token


def parse_instrument_token(token: str) -> Tuple[str, str]:
    """
    Parse a token into (exchange, symbol).
    'NSE:RELIANCE' -> ('NSE', 'RELIANCE')
    'RELIANCE' -> ('NSE', 'RELIANCE')
    """
    normalized = normalize_instrument_token(token)
    if ":" in normalized:
        parts = normalized.split(":", 1)
        return parts[0], parts[1]
    return "NSE", normalized


def is_option_contract(token: str) -> bool:
    """
    Determine if token is an F&O option contract (CE/PE with strike price).
    Correctly rejects underlying equities ending in 'CE' (e.g. 'RELIANCE', 'VOLTAMP').
    """
    if not token:
        return False
    _, symbol = parse_instrument_token(token)
    clean_sym = symbol.upper().replace(" ", "")
    return bool(_OPTION_REGEX.match(clean_sym))


def is_index_token(token: str) -> bool:
    """
    Determine if token represents an index (e.g. Nifty 50).
    """
    normalized = normalize_instrument_token(token)
    if normalized.startswith("NSE_INDEX:"):
        return True
    _, sym = parse_instrument_token(token)
    return sym.upper() in _INDEX_NAMES


_OPTION_DETAILS_REGEX = re.compile(r"^([A-Z]+)(\d{2}[A-Z0-9]{3})?(\d+(?:\.\d+)?)(CE|PE)$", re.IGNORECASE)


def parse_option_details(token: str) -> Optional[Tuple[str, float, str]]:
    """
    Extract (underlying_symbol, strike, option_type) from contract token.
    Returns None if not a valid option contract.
    Examples:
        'NSE:RELIANCE26NOV2500CE' -> ('RELIANCE', 2500.0, 'CE')
        'TCS26NOV3500PE' -> ('TCS', 3500.0, 'PE')
    """
    if not is_option_contract(token):
        return None
    _, sym = parse_instrument_token(token)
    clean_sym = sym.upper().replace(" ", "")
    m = _OPTION_DETAILS_REGEX.search(clean_sym)
    if not m:
        return None
    underlying = m.group(1)
    strike = float(m.group(3))
    option_type = m.group(4).upper()
    return underlying, strike, option_type
