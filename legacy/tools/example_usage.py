#!/usr/bin/env python3
"""
example_usage.py
================
Demonstrates various ways to use `filter_instruments.py` in your own Python
trading algorithms, backtesting workflows, or analysis scripts.

Run with:
    python3 example_usage.py
"""

from filter_instruments import load_instruments


def main():
    print("=" * 70)
    print("Loading instruments file...")
    print("=" * 70)
    instruments = load_instruments("instrument.csv")
    print(f"Total loaded instruments: {len(instruments):,}\n")

    # -------------------------------------------------------------------------
    # Example 1: Discover All Unique FNO Underlyings
    # -------------------------------------------------------------------------
    print("1. Exploring Unique FNO Underlyings:")
    fno_underlyings = instruments.get_underlyings(segment="FNO")
    print(f"   Found {len(fno_underlyings)} unique underlyings in FNO segment.")
    print(f"   Sample FNO underlyings (first 12):")
    for sym in fno_underlyings[:12]:
        name = instruments.get_underlying_name(sym)
        name_str = f"({name})" if name and name != sym else ""
        print(f"     - {sym:<15} {name_str}")
    print()

    # -------------------------------------------------------------------------
    # Example 2: Detailed Contract Breakdown for Top FNO Underlyings
    # -------------------------------------------------------------------------
    print("2. Detailed FNO Breakdown (Top 5 underlyings by contract volume):")
    summary = instruments.get_underlying_summary(segment="FNO")
    header = f"   {'Underlying':<12} | {'Name':<25} | {'Total':>6} | {'CE':>5} | {'PE':>5} | {'FUT':>4} | {'Nearest Expiry':<12}"
    print(header)
    print("   " + "-" * (len(header) - 3))
    for item in summary[:5]:
        print(
            f"   {item['underlying_symbol']:<12} | "
            f"{item['name'][:25]:<25} | "
            f"{item['total_contracts']:>6} | "
            f"{item['ce_count']:>5} | "
            f"{item['pe_count']:>5} | "
            f"{item['fut_count']:>4} | "
            f"{item['nearest_expiry']:<12}"
        )
    print()

    # -------------------------------------------------------------------------
    # Example 3: Cash Equity with Company Names (e.g. Edelweiss, Navi, Muthoot)
    # -------------------------------------------------------------------------
    print("3. Fetching Cash Equity (EQ) instruments with Company Names:")
    bse_cash = instruments.filter(segment="CASH", query="EDELWEISS", exchange="BSE")
    print(f"   Found {len(bse_cash)} Edelweiss cash instruments on BSE. First 4:")
    for eq in bse_cash[:4]:
        print(f"   {eq.trading_symbol:<14} | Name: {eq.name:<40} | Token: {eq.exchange_token}")
    print()

    # -------------------------------------------------------------------------
    # Example 4: Available Expiry Dates for NIFTY & BANKNIFTY
    # -------------------------------------------------------------------------
    print("4. Fetching available expiry dates:")
    nifty_expiries = instruments.get_expiries("NIFTY")
    banknifty_expiries = instruments.get_expiries("BANKNIFTY")
    print(f"   NIFTY expiries ({len(nifty_expiries)}): {nifty_expiries[:4]} ...")
    print(f"   BANKNIFTY expiries ({len(banknifty_expiries)}): {banknifty_expiries[:4]} ...\n")

    # -------------------------------------------------------------------------
    # Example 5: Active NIFTY Futures
    # -------------------------------------------------------------------------
    print("5. Active NIFTY Futures contracts:")
    nifty_futs = instruments.get_futures("NIFTY")
    for fut in nifty_futs[:3]:
        print(f"   Symbol: {fut.trading_symbol:<20} | Name: {fut.display_name:<10} | Expiry: {fut.expiry_date} | Lot: {fut.lot_size}")
    print()

    # -------------------------------------------------------------------------
    # Example 6: NIFTY Options Filter (with Name & Strikes)
    # -------------------------------------------------------------------------
    nearest_expiry = nifty_expiries[0] if nifty_expiries else "2026-10-29"
    print(f"6. Filtering NIFTY Options for expiry {nearest_expiry} between strikes 24,500 and 25,500:")
    calls_puts = instruments.filter(
        underlying_symbol="NIFTY",
        expiry_date=nearest_expiry,
        min_strike=24500,
        max_strike=25500,
    )
    print(f"   Found {len(calls_puts)} matching option contracts.")
    for opt in calls_puts[:4]:
        print(f"   {opt.trading_symbol:<22} | Name: {opt.display_name:<10} | Type: {opt.instrument_type} | Strike: {opt.strike_price:>7.1f} | Token: {opt.exchange_token}")
    print()

    # -------------------------------------------------------------------------
    # Example 7: Option Chain Matrix (Paired CE & PE)
    # -------------------------------------------------------------------------
    print("7. Option Chain (Strike-matched CE and PE):")
    chain = instruments.get_option_chain("NIFTY", expiry=nearest_expiry)
    print(f"   Total strikes in chain: {len(chain)}")
    mid = len(chain) // 2
    subset = chain[max(0, mid - 2) : min(len(chain), mid + 3)]
    print(f"   {'CE Symbol':<22} | {'Strike':^10} | {'PE Symbol':<22}")
    print("   " + "-" * 60)
    for row in subset:
        ce_sym = row["CE"].trading_symbol if row["CE"] else "-"
        pe_sym = row["PE"].trading_symbol if row["PE"] else "-"
        print(f"   {ce_sym:<22} | {row['strike_price']:^10.1f} | {pe_sym:<22}")
    print()

    # -------------------------------------------------------------------------
    # Example 8: Exporting Filtered Data
    # -------------------------------------------------------------------------
    export_file = "nifty_sample_filtered.csv"
    print(f"8. Exporting filtered contracts to '{export_file}'...")
    instruments.export_csv(calls_puts, export_file)
    print(f"   Saved {len(calls_puts)} rows to {export_file}.\n")

    print("All examples completed successfully!")


if __name__ == "__main__":
    main()
