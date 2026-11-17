import json
from app import app, get_db_connection

def test_my_bookings_refinement():
    print("=== TESTING MY BOOKINGS REFINEMENT & RESERVATION MANAGEMENT ===")
    client = app.test_client()

    with app.app_context():
        # Authenticate as CUST-1001 (Rohit Sharma)
        client.get('/switch-user/CUST-1001')

        # 1. Header Validation: BOOK ROOM button removed
        print("\n1. Verifying Header navigation...")
        res = client.get('/')
        assert res.status_code == 200
        # Verify Book Room button was removed from header actions
        assert b'class="btn btn-gold btn-header"' not in res.data, "BOOK ROOM button should be removed from header!"
        assert b'Rooms &amp; Suites' in res.data or b'Rooms & Suites' in res.data
        assert b'My Bookings' in res.data
        assert b'Room Service' in res.data
        assert b'href="/dining"' not in res.data, "Dining link should be permanently removed!"
        print("  [PASS] Header navigation verified: BOOK ROOM button cleanly removed, Rooms & Suites remains primary entry.")

        # 2. My Bookings Page Verification
        print("\n2. Verifying My Bookings page layout...")
        res = client.get('/my-bookings')
        assert res.status_code == 200
        content = res.get_data(as_text=True)
        assert "CUSTOMER PORTAL" in content
        assert "My Reservations" in content
        assert "Manage your stays, payments, and hotel services." in content
        assert "Book New Room" in content
        assert "Edit Profile" in content

        # Check clean card structure
        assert "reservation-card" in content
        assert "card-sec-room" in content
        assert "card-sec-stay" in content
        assert "card-sec-actions" in content

        # Check that individual cluttered buttons are NOT on card
        # Buttons removed from card: Ledger, Payments, Food Menu, View Invoice, Voucher, Cancel
        assert 'title="View Folio & Itemized Expenses"' not in content
        assert 'title="Make Partial or Full Payment"' not in content
        assert 'title="Download / Print Final Invoice"' not in content
        assert 'title="View Voucher"' not in content

        # Check that View Details and Order Food ARE present
        assert "View Details" in content
        assert "Order Food" in content
        print("  [PASS] My Bookings page verified: Horizontal 3-section card layout, clutter removed, View Details and Order Food prominent.")

        # 3. Dedicated Booking Details Page Verification
        print("\n3. Verifying Dedicated Booking Details Page...")
        res = client.get('/my-bookings/7')
        assert res.status_code == 200
        content = res.get_data(as_text=True)
        assert "Reservation Management" in content
        assert "Total Charges" in content
        assert "Amount Paid" in content
        assert "Outstanding Balance" in content
        assert "Stay Details" in content
        assert "Payments" in content
        assert "Expense Ledger" in content
        assert "Dining &amp; Room Service" in content or "Dining & Room Service" in content
        assert "Invoice &amp; Voucher" in content or "Invoice & Voucher" in content
        assert "Cancellation" in content
        assert "Room #" in content
        print("  [PASS] Booking details page (/my-bookings/7) rendered with all 6 organized sections, tabs, and financial summary.")

        # 4. Order Food Upfront Association
        print("\n4. Verifying Order Food association...")
        # Use Booking 9 which is 'Checked In' or Booking 10 which is 'Confirmed'
        res = client.get('/dining/menu?booking_id=9')
        assert res.status_code == 200
        content = res.get_data(as_text=True)
        assert "Deliver To Room" in content
        assert "Room #" in content
        assert "selected" in content or "value=\"9\"" in content
        print("  [PASS] Order Food correctly links with preselected reservation, guest folio, and assigned room number.")

    print("\n=== ALL REFINEMENT TESTS PASSED! ===")

if __name__ == '__main__':
    test_my_bookings_refinement()
