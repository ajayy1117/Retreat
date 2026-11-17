import json
from app import app, get_db_connection
from ledger_engine import calculate_booking_financials, format_inr

def test_financial_suite():
    print("=== STARTING PHASE 5 AUTOMATED VERIFICATION SUITE ===")
    client = app.test_client()

    with app.app_context():
        # Authenticate as CUST-1001 (Rohit Sharma)
        client.get('/switch-user/CUST-1001')

        # 1. Test Navigation & Customer Endpoints
        print("\n1. Testing customer endpoints...")
        res = client.get('/my-bookings')
        assert res.status_code == 200, f"Expected 200 on /my-bookings, got {res.status_code}"
        assert b"View Details" in res.data
        assert b"Order Food" in res.data
        print("  [PASS] /my-bookings rendered with clean View Details and Order Food action buttons.")

        # Test Booking Details Page
        res = client.get('/my-bookings/7')
        assert res.status_code == 200
        assert b"Expense Ledger" in res.data
        assert b"Payments" in res.data
        print("  [PASS] /my-bookings/7 dedicated details page rendered with Ledger and Payments tabs.")

        res = client.get('/dining/menu')
        assert res.status_code == 200, f"Expected 200 on /dining/menu, got {res.status_code}"
        assert b"Gourmet Room Service" in res.data
        assert b"Room Service Cart" in res.data
        print("  [PASS] /dining/menu rendered with interactive cart drawer.")

        # Test Booking 7 Payments
        res = client.get('/bookings/7/payments')
        assert res.status_code == 200, f"Expected 200 on /bookings/7/payments, got {res.status_code}"
        assert b"Booking Payment & Settlement" in res.data
        print("  [PASS] /bookings/7/payments rendered.")

        # Test Booking 7 Ledger
        res = client.get('/bookings/7/ledger')
        assert res.status_code == 200, f"Expected 200 on /bookings/7/ledger, got {res.status_code}"
        assert b"Itemized Folio Statement" in res.data
        print("  [PASS] /bookings/7/ledger rendered.")

        # Test Booking 7 Checkout
        res = client.get('/bookings/7/checkout')
        assert res.status_code == 200, f"Expected 200 on /bookings/7/checkout, got {res.status_code}"
        assert b"Checkout Billing Statement" in res.data
        print("  [PASS] /bookings/7/checkout rendered.")

        # 2. Test Partial & Full Payment Workflow
        print("\n2. Testing payment transactions...")
        # Post partial payment of 1,000 on Booking 7
        res = client.post('/bookings/7/payments', data={
            'payment_type': 'partial',
            'amount': '1000',
            'payment_method': 'UPI',
            'notes': 'Test partial deposit payment via UPI'
        }, follow_redirects=True)
        assert res.status_code == 200
        assert b"was successful" in res.data
        print("  [PASS] Partial payment of ₹1,000 processed.")

        # 3. Test Room Service Order API
        print("\n3. Testing room service ordering API...")
        order_payload = {
            'booking_id': 7,
            'delivery_notes': 'Please deliver to room 302 hot with extra lime',
            'items': [
                {'id': 1, 'qty': 2},
                {'id': 6, 'qty': 1}
            ]
        }
        res = client.post('/api/order-food', json=order_payload)
        assert res.status_code == 200
        order_data = res.get_json()
        assert order_data['status'] == 'success'
        order_ref = order_data['order_ref']
        print(f"  [PASS] Food order placed: {order_ref} with status 'Placed'.")

        # Verify that 'Placed' order has NOT been added to expenses ledger
        conn = get_db_connection()
        ord_row = conn.execute('SELECT * FROM food_orders WHERE order_ref = ?', (order_ref,)).fetchone()
        exp_row = conn.execute('SELECT * FROM expenses WHERE order_id = ?', (ord_row['id'],)).fetchone()
        assert exp_row is None, "Placed order should NOT be added to expenses ledger yet!"
        print("  [PASS] Verified: Pending order is NOT on guest ledger.")

        # 4. Test Admin Food Order Status Update -> Delivered -> Ledger Entry
        print("\n4. Testing admin status change to Delivered...")
        res = client.post(f"/admin/food-orders/{ord_row['id']}/update-status", data={
            'order_status': 'Delivered'
        }, follow_redirects=True)
        assert res.status_code == 200
        exp_row_after = conn.execute('SELECT * FROM expenses WHERE order_id = ?', (ord_row['id'],)).fetchone()
        assert exp_row_after is not None, "Delivered order MUST be added to expenses ledger!"
        assert exp_row_after['category'] == 'Room Service'
        print(f"  [PASS] Order delivered and automatically finalized in ledger as {exp_row_after['expense_ref']}.")

        # 5. Test Admin Manual Expense Addition
        print("\n5. Testing admin manual expense addition...")
        # Switch to admin user
        client.get('/switch-user/ADMIN-001')
        res = client.post('/admin/expenses', data={
            'booking_id': 7,
            'category': 'Laundry',
            'description': 'Express Dry Cleaning 2 Suits',
            'quantity': 2,
            'unit_price': 350
        }, follow_redirects=True)
        assert res.status_code == 200
        assert b"Manual expense" in res.data
        print("  [PASS] Admin manual expense added to folio.")

        # Switch back to customer for checkout
        client.get('/switch-user/CUST-1001')

        # 6. Test Checkout Balance Requirement
        print("\n6. Testing checkout balance enforcement...")
        # Attempt checkout when balance > 0
        res = client.post('/bookings/7/checkout', data={'action': 'complete_checkout'}, follow_redirects=True)
        assert res.status_code == 200
        assert b"Cannot complete checkout" in res.data
        print("  [PASS] Checkout properly blocked when outstanding balance > 0.")

        # Settle balance at checkout
        res = client.post('/bookings/7/checkout', data={'action': 'pay_checkout', 'payment_method': 'Credit/Debit Card'}, follow_redirects=True)
        assert res.status_code == 200
        assert b"confirmed via Credit/Debit Card" in res.data
        print("  [PASS] Outstanding balance settled via checkout pay action.")

        # Now complete checkout
        res = client.post('/bookings/7/checkout', data={'action': 'complete_checkout'}, follow_redirects=True)
        assert res.status_code == 200
        assert b"TAX INVOICE" in res.data
        assert b"PAID &amp; SETTLED" in res.data or b"PAID & SETTLED" in res.data
        print("  [PASS] Checkout completed, stay marked 'Checked Out', final invoice generated.")

        # Verify DB state of booking 7
        b_final = conn.execute('SELECT * FROM bookings WHERE id = 7').fetchone()
        assert b_final['status'] == 'Checked Out'
        assert b_final['payment_status'] == 'Paid'
        inv_final = conn.execute('SELECT * FROM invoices WHERE booking_id = 7').fetchone()
        assert inv_final is not None
        assert inv_final['outstanding_balance'] == 0
        print(f"  [PASS] Final invoice verified: {inv_final['invoice_ref']}.")

        conn.close()

    print("\n=== ALL VERIFICATION TESTS PASSED SUCCESSFULLY! ===")

if __name__ == '__main__':
    test_financial_suite()
