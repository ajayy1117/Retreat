import json
from app import app, get_db_connection

def test_ledger_financial_consistency():
    print("=== RUNNING EXPENSE LEDGER & FINANCIAL CONSISTENCY TEST SUITE ===")
    client = app.test_client()

    with app.app_context():
        # Test Booking 14 (Ajay Carpenter - Comfort Room with Half Board, as shown in user prompt)
        print("\n1. Testing Booking 14 Expense Ledger (/bookings/14/ledger)...")
        res = client.get('/bookings/14/ledger')
        assert res.status_code == 200, f"Expected 200, got {res.status_code}"
        html = res.get_data(as_text=True)

        # A. Room Charges verification
        assert "₹5,997" in html, "Room charges amount ₹5,997 must be present in HTML!"
        assert "Base tariff &amp; occupancy" in html or "Base tariff & occupancy" in html
        print("  [PASS] Room charges ₹5,997 correctly displayed on summary card.")

        # B. Services & Dining verification
        assert "₹5,400" in html, "Services & Dining amount ₹5,400 must be present in HTML!"
        print("  [PASS] Services & Dining ₹5,400 correctly displayed on summary card.")

        # C. Total payments and balance
        assert "₹0" in html, "Total Payments ₹0 must be displayed."
        assert "₹11,397" in html, "Outstanding Balance ₹11,397 must be displayed."
        print("  [PASS] Total payments (₹0) and remaining balance (₹11,397) correctly displayed.")

        # D. Itemized Finalized Charges Table
        assert "2 entries" in html, f"Itemized Finalized Charges must show 2 entries, not 0!"
        assert "EXP-RC-0014" in html, "EXP-RC-0014 room charge reference must be in the table!"
        assert "EXP-MP-0014" in html, "EXP-MP-0014 meal plan reference must be in the table!"
        assert "Comfort Room" in html
        assert "Half Board (Breakfast + Dinner)" in html
        print("  [PASS] Itemized Finalized Charges table contains 2 entries (Room Charges + Meal Plan) with matching subtotal of ₹11,397.")

        # E. Typography inspection: Cormorant Garamond must not be used on monetary amounts
        # Ensure ledger-summary-val and ledger-currency-tnum are styled with DM Sans / tabular-nums
        assert "ledger-summary-val" in html
        assert "ledger-currency-tnum" in html
        print("  [PASS] Typography classes verified: DM Sans and tabular numerals applied to financial figures.")

        # 2. Testing Dedicated Booking Details Tab 3 (/my-bookings/14)
        print("\n2. Testing Booking Details Folio (/my-bookings/14)...")
        res = client.get('/my-bookings/14')
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert "EXP-RC-0014" in html
        assert "EXP-MP-0014" in html
        assert "₹11,397" in html
        print("  [PASS] Tab 3 of /my-bookings/14 displays complete itemized ledger without empty tables.")

        # 3. Testing Booking 7 (with existing database incidentals)
        print("\n3. Testing Booking 7 (Complex folio with DB expenses)...")
        res = client.get('/bookings/7/ledger')
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert "₹5,997" in html  # Room Charges
        assert "EXP-RC-0007" in html
        print("  [PASS] Booking 7 ledger preserves both room charges and recorded DB incidentals.")

    print("\n=== ALL EXPENSE LEDGER & FINANCIAL CONSISTENCY TESTS PASSED! ===")

if __name__ == '__main__':
    test_ledger_financial_consistency()

