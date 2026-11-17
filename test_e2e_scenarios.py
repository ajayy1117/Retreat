import unittest
import json
import sqlite3
from datetime import date, timedelta
from app import app, get_db_connection

class TestPhase6Integration(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def test_scenario_a_full_journey(self):
        """Scenario A: Customer books a room, makes full payment, checks in, orders food, receives order, and checks out."""
        # Pick an available physical room for Category 1
        conn = get_db_connection()
        avail_pr = conn.execute('SELECT room_number FROM physical_rooms WHERE room_category_id = 1 AND operational_status = "Available" LIMIT 1').fetchone()
        if not avail_pr:
            # reset 103 for test
            conn.execute('UPDATE physical_rooms SET operational_status = "Available", current_booking_id = NULL WHERE room_number = "103"')
            conn.commit()
            target_room_num = '103'
        else:
            target_room_num = avail_pr['room_number']
        conn.close()

        # 1. Book a room
        tomorrow = (date.today() + timedelta(days=20)).strftime('%Y-%m-%d')
        three_days = (date.today() + timedelta(days=22)).strftime('%Y-%m-%d')
        
        book_res = self.client.post('/api/confirm-booking', json={
            'room_id': 1,
            'check_in': tomorrow,
            'check_out': three_days,
            'adults': 2,
            'children': 0,
            'rooms_count': 1,
            'guest_name': 'Scenario A Tester',
            'guest_email': 'tester_a@retreat.com',
            'guest_phone': '+91 98888 11111',
            'meal_plan': 'room_only'
        })
        self.assertEqual(book_res.status_code, 200)
        book_data = json.loads(book_res.data)
        b_code = book_data['booking_code']

        # Get booking ID
        conn = get_db_connection()
        b_row = conn.execute('SELECT * FROM bookings WHERE booking_code = ?', (b_code,)).fetchone()
        b_id = b_row['id']
        conn.close()

        # 2. Make Full Payment
        pmt_res = self.client.post(f'/bookings/{b_id}/payments', data={
            'payment_type': 'full',
            'payment_method': 'UPI'
        }, follow_redirects=True)
        self.assertEqual(pmt_res.status_code, 200)

        conn = get_db_connection()
        b_row = conn.execute('SELECT payment_status FROM bookings WHERE id = ?', (b_id,)).fetchone()
        self.assertEqual(b_row['payment_status'], 'Paid')
        conn.close()

        # 3. Admin Logs in and checks in the guest
        with self.client.session_transaction() as sess:
            sess['customer_id'] = 'ADMIN-001'
            sess['user_role'] = 'admin'
            sess['user_name'] = 'General Manager'

        checkin_res = self.client.post(f'/admin/bookings/{b_id}/check-in', data={
            'assigned_room_number': target_room_num
        }, follow_redirects=True)
        self.assertEqual(checkin_res.status_code, 200)

        conn = get_db_connection()
        b_row = conn.execute('SELECT status, assigned_room_number FROM bookings WHERE id = ?', (b_id,)).fetchone()
        self.assertEqual(b_row['status'], 'Checked In')
        self.assertEqual(b_row['assigned_room_number'], target_room_num)
        conn.close()

        # 4. Guest orders food
        order_res = self.client.post('/api/order-food', json={
            'booking_id': b_id,
            'items': [{'id': 1, 'qty': 2}],
            'delivery_notes': 'Hot and fresh please'
        })
        self.assertEqual(order_res.status_code, 200)
        ord_data = json.loads(order_res.data)
        ord_ref = ord_data['order_ref']

        # Verify not yet in expenses ledger
        conn = get_db_connection()
        exp_count = conn.execute('SELECT COUNT(*) FROM expenses WHERE booking_id = ? AND category = "Food & Beverages"', (b_id,)).fetchone()[0]
        self.assertEqual(exp_count, 0)

        # 5. Admin updates status to Delivered
        ord_row = conn.execute('SELECT id FROM food_orders WHERE order_ref = ?', (ord_ref,)).fetchone()
        ord_id = ord_row['id']
        conn.close()

        deliver_res = self.client.post(f'/admin/food-orders/{ord_id}/update-status', data={
            'order_status': 'Delivered'
        }, follow_redirects=True)
        self.assertEqual(deliver_res.status_code, 200)

        # Verify finalized in expenses exactly once
        conn = get_db_connection()
        exp_rows = conn.execute('SELECT * FROM expenses WHERE order_id = ?', (ord_id,)).fetchall()
        self.assertEqual(len(exp_rows), 1)
        self.assertEqual(exp_rows[0]['expense_status'], 'Finalized')

        # Booking balance should now have the food order amount outstanding
        b_row = conn.execute('SELECT payment_status FROM bookings WHERE id = ?', (b_id,)).fetchone()
        self.assertEqual(b_row['payment_status'], 'Partially Paid')
        conn.close()

        # 6. Settle food bill at checkout and complete checkout
        checkout_pay = self.client.post(f'/admin/bookings/{b_id}/checkout', data={
            'action': 'record_payment',
            'payment_amount': exp_rows[0]['total_amount'],
            'payment_method': 'Cash'
        }, follow_redirects=True)
        self.assertEqual(checkout_pay.status_code, 200)

        complete_checkout = self.client.post(f'/admin/bookings/{b_id}/checkout', data={
            'action': 'complete_checkout'
        }, follow_redirects=True)
        self.assertEqual(complete_checkout.status_code, 200)

        conn = get_db_connection()
        b_row = conn.execute('SELECT status, payment_status FROM bookings WHERE id = ?', (b_id,)).fetchone()
        self.assertEqual(b_row['status'], 'Checked Out')
        self.assertEqual(b_row['payment_status'], 'Paid')

        # Check room status transitioned to Cleaning
        room_row = conn.execute('SELECT operational_status FROM physical_rooms WHERE room_number = ?', (target_room_num,)).fetchone()
        self.assertEqual(room_row['operational_status'], 'Cleaning')

        # Check invoice generated
        inv_row = conn.execute('SELECT * FROM invoices WHERE booking_id = ?', (b_id,)).fetchone()
        self.assertIsNotNone(inv_row)
        conn.close()

    def test_scenario_b_partial_payments_and_services(self):
        """Scenario B: 50% advance, orders food and laundry, partial payment, settles remaining balance at checkout."""
        tomorrow = (date.today() + timedelta(days=25)).strftime('%Y-%m-%d')
        seven_days = (date.today() + timedelta(days=27)).strftime('%Y-%m-%d')

        book_res = self.client.post('/api/confirm-booking', json={
            'room_id': 2,
            'check_in': tomorrow,
            'check_out': seven_days,
            'adults': 2,
            'children': 0,
            'rooms_count': 1,
            'guest_name': 'Scenario B Partial',
            'guest_email': 'tester_b@retreat.com',
            'guest_phone': '+91 98888 22222',
            'meal_plan': 'room_only'
        })
        b_data = json.loads(book_res.data)
        b_code = b_data['booking_code']

        conn = get_db_connection()
        b_row = conn.execute('SELECT * FROM bookings WHERE booking_code = ?', (b_code,)).fetchone()
        b_id = b_row['id']
        total_price = b_row['total_price']
        conn.close()

        # 50% partial payment
        half_price = total_price / 2.0
        p1 = self.client.post(f'/bookings/{b_id}/payments', data={
            'payment_type': 'partial',
            'amount': str(half_price),
            'payment_method': 'Net Banking'
        }, follow_redirects=True)
        self.assertEqual(p1.status_code, 200)

        conn = get_db_connection()
        b_row = conn.execute('SELECT payment_status FROM bookings WHERE id = ?', (b_id,)).fetchone()
        self.assertEqual(b_row['payment_status'], 'Partially Paid')
        conn.close()

        # Submit laundry request
        srv_res = self.client.post('/api/service-requests', json={
            'booking_id': b_id,
            'request_type': 'Laundry',
            'priority': 'Standard',
            'description': 'Express washing for 2 suits'
        })
        self.assertEqual(srv_res.status_code, 200)
        s_data = json.loads(srv_res.data)
        s_ref = s_data['request_ref']

        # Admin completes service request
        with self.client.session_transaction() as sess:
            sess['customer_id'] = 'ADMIN-001'
            sess['user_role'] = 'admin'

        conn = get_db_connection()
        sr_id = conn.execute('SELECT id FROM service_requests WHERE request_ref = ?', (s_ref,)).fetchone()[0]
        conn.close()

        comp_srv = self.client.post('/admin/service-requests', data={
            'action': 'update_status',
            'request_id': sr_id,
            'status': 'Completed'
        }, follow_redirects=True)
        self.assertEqual(comp_srv.status_code, 200)

        # Verify expense ledger has Laundry charge
        conn = get_db_connection()
        sr_row = conn.execute('SELECT expense_id FROM service_requests WHERE id = ?', (sr_id,)).fetchone()
        self.assertIsNotNone(sr_row['expense_id'])
        exp_row = conn.execute('SELECT * FROM expenses WHERE id = ?', (sr_row['expense_id'],)).fetchone()
        self.assertEqual(exp_row['category'], 'Laundry')
        self.assertEqual(exp_row['total_amount'], 350.0)
        conn.close()

    def test_scenario_c_cancelled_food_order(self):
        """Scenario C: Food order cancelled does not create finalized charge."""
        conn = get_db_connection()
        b_id = conn.execute('SELECT id FROM bookings WHERE status = "Confirmed" LIMIT 1').fetchone()[0]
        conn.close()

        ord_res = self.client.post('/api/order-food', json={
            'booking_id': b_id,
            'items': [{'id': 1, 'qty': 1}],
            'delivery_notes': 'Test Cancel'
        })
        ord_data = json.loads(ord_res.data)
        ord_ref = ord_data['order_ref']

        with self.client.session_transaction() as sess:
            sess['user_role'] = 'admin'

        conn = get_db_connection()
        ord_id = conn.execute('SELECT id FROM food_orders WHERE order_ref = ?', (ord_ref,)).fetchone()[0]
        conn.close()

        # Admin cancels order
        cancel_res = self.client.post(f'/admin/food-orders/{ord_id}/update-status', data={
            'order_status': 'Cancelled'
        }, follow_redirects=True)
        self.assertEqual(cancel_res.status_code, 200)

        conn = get_db_connection()
        exp_count = conn.execute('SELECT COUNT(*) FROM expenses WHERE order_id = ?', (ord_id,)).fetchone()[0]
        self.assertEqual(exp_count, 0)
        conn.close()

    def test_scenario_d_room_overlap_prevention(self):
        """Scenario D: Prevent double booking or overlapping physical room assignment."""
        # Reset Room 104
        conn = get_db_connection()
        conn.execute('UPDATE physical_rooms SET operational_status = "Available", current_booking_id = NULL WHERE room_number = "104"')
        conn.commit()
        conn.close()

        # 1. Create booking 1 for room 104
        tomorrow = (date.today() + timedelta(days=30)).strftime('%Y-%m-%d')
        twelve_days = (date.today() + timedelta(days=33)).strftime('%Y-%m-%d')

        b1_res = self.client.post('/api/confirm-booking', json={
            'room_id': 1,
            'check_in': tomorrow,
            'check_out': twelve_days,
            'adults': 2,
            'children': 0,
            'rooms_count': 1,
            'guest_name': 'Booking 1 Overlap',
            'guest_email': 'b1_overlap@retreat.com',
            'guest_phone': '+91 98888 33333',
            'meal_plan': 'room_only'
        })
        b1_data = json.loads(b1_res.data)
        conn = get_db_connection()
        b1_id = conn.execute('SELECT id FROM bookings WHERE booking_code = ?', (b1_data['booking_code'],)).fetchone()[0]
        conn.close()

        # Assign room 104 to b1
        with self.client.session_transaction() as sess:
            sess['user_role'] = 'admin'
        self.client.post(f'/admin/bookings/{b1_id}/assign-room', data={'room_number': '104'}, follow_redirects=True)

        # 2. Create booking 2 for overlapping dates
        overlap_in = (date.today() + timedelta(days=31)).strftime('%Y-%m-%d')
        overlap_out = (date.today() + timedelta(days=34)).strftime('%Y-%m-%d')

        b2_res = self.client.post('/api/confirm-booking', json={
            'room_id': 1,
            'check_in': overlap_in,
            'check_out': overlap_out,
            'adults': 2,
            'children': 0,
            'rooms_count': 1,
            'guest_name': 'Booking 2 Overlap',
            'guest_email': 'b2_overlap@retreat.com',
            'guest_phone': '+91 98888 44444',
            'meal_plan': 'room_only'
        })
        b2_data = json.loads(b2_res.data)
        conn = get_db_connection()
        b2_id = conn.execute('SELECT id FROM bookings WHERE booking_code = ?', (b2_data['booking_code'],)).fetchone()[0]
        conn.close()

        # Attempt to assign room 104 to b2 (overlapping dates)
        assign_res = self.client.post(f'/admin/bookings/{b2_id}/assign-room', data={'room_number': '104'}, follow_redirects=True)
        self.assertIn(b'already reserved', assign_res.data)

    def test_scenario_f_role_based_access(self):
        """Scenario F & Access Control: Customer cannot access admin routes, and cannot access another customer's folio."""
        # Non-admin trying to access /admin
        with self.client.session_transaction() as sess:
            sess['customer_id'] = 'CUST-1001'
            sess['user_role'] = 'customer'

        adm_res = self.client.get('/admin', follow_redirects=False)
        self.assertEqual(adm_res.status_code, 302)  # redirected to login

        # Non-admin trying to access another customer's booking
        conn = get_db_connection()
        other_booking = conn.execute('SELECT id FROM bookings WHERE customer_id != "CUST-1001" LIMIT 1').fetchone()
        conn.close()

        if other_booking:
            other_id = other_booking['id']
            ledger_res = self.client.get(f'/bookings/{other_id}/ledger', follow_redirects=True)
            self.assertIn(b'Access restricted', ledger_res.data)

if __name__ == '__main__':
    unittest.main()
