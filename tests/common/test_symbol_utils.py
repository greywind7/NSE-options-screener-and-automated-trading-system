"""
Unit Tests for Canonical Symbol Utilities.
"""
import unittest
from core.common.symbol_utils import (
    normalize_instrument_token,
    parse_instrument_token,
    is_option_contract,
    is_index_token,
    parse_option_details,
)


class TestSymbolUtils(unittest.TestCase):
    def test_normalize_instrument_token(self):
        cases = [
            ("NSE_EQ|RELIANCE", "NSE:RELIANCE"),
            ("NSE_FO|TCS26NOV3500CE", "NSE:TCS26NOV3500CE"),
            ("NSE:RELIANCE", "NSE:RELIANCE"),
            ("NSE_INDEX|Nifty 50", "NSE_INDEX:Nifty 50"),
            ("BSE_EQ|500325", "BSE:500325"),
            ("MCX_FO|CRUDEOIL24NOV", "MCX:CRUDEOIL24NOV"),
            ("MCX:GOLD24DEC", "MCX:GOLD24DEC"),
            ("", ""),
            ("   NSE_EQ|INFY   ", "NSE:INFY"),
        ]
        for inp, expected in cases:
            with self.subTest(inp=inp):
                self.assertEqual(normalize_instrument_token(inp), expected)

    def test_parse_instrument_token(self):
        self.assertEqual(parse_instrument_token("NSE:RELIANCE"), ("NSE", "RELIANCE"))
        self.assertEqual(parse_instrument_token("NSE_EQ|SBIN"), ("NSE", "SBIN"))
        self.assertEqual(parse_instrument_token("TCS"), ("NSE", "TCS"))

    def test_is_option_contract(self):
        self.assertTrue(is_option_contract("NSE_FO|TCS26NOV3500CE"))
        self.assertTrue(is_option_contract("NSE:TCS26NOV3500PE"))
        self.assertTrue(is_option_contract("NIFTY24NOV24000CE"))
        self.assertFalse(is_option_contract("NSE:RELIANCE"))
        self.assertFalse(is_option_contract("NSE_EQ|INFY"))
        self.assertFalse(is_option_contract("NSE_INDEX|Nifty 50"))

    def test_is_index_token(self):
        self.assertTrue(is_index_token("NSE_INDEX|Nifty 50"))
        self.assertTrue(is_index_token("NSE:Nifty 50"))
        self.assertTrue(is_index_token("BANKNIFTY"))
        self.assertFalse(is_index_token("NSE:RELIANCE"))
        self.assertFalse(is_index_token("NSE_FO|TCS26NOV3500CE"))

    def test_parse_option_details(self):
        res1 = parse_option_details("NSE:RELIANCE26NOV2500CE")
        self.assertEqual(res1, ("RELIANCE", 2500.0, "CE"))

        res2 = parse_option_details("TCS26NOV3500PE")
        self.assertEqual(res2, ("TCS", 3500.0, "PE"))

        self.assertIsNone(parse_option_details("NSE:RELIANCE"))


if __name__ == "__main__":
    unittest.main()

