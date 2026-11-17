import sqlite3
import json

conn = sqlite3.connect('retreat.db')
c = conn.cursor()

c.execute('SELECT id, booking_code, customer_id, total_price, base_room_charges, meal_plan, meal_plan_charge FROM bookings LIMIT 5')
sample_books = c.fetchall()

c.execute('DELETE FROM expenses')
c.execute('DELETE FROM payments')
c.execute('DELETE FROM food_orders')

for b in sample_books:
    bid, bcode, cid, tot, base_charge, meal_plan, meal_charge = b
    base = base_charge or tot
    c.execute('''INSERT INTO expenses (expense_ref, booking_id, customer_id, category, description, quantity, unit_price, total_amount, expense_status)
                 VALUES (?, ?, ?, 'Room Charges', 'Base Room Accommodation Charges', 1, ?, ?, 'Finalized')''',
              (f'EXP-RC-{bid:04d}', bid, cid, base, base))
    
    if meal_charge and meal_charge > 0:
        c.execute('''INSERT INTO expenses (expense_ref, booking_id, customer_id, category, description, quantity, unit_price, total_amount, expense_status)
                     VALUES (?, ?, ?, 'Meal Plan', ?, 1, ?, ?, 'Finalized')''',
                  (f'EXP-MP-{bid:04d}', bid, cid, f'Meal Plan ({meal_plan}) Package', meal_charge, meal_charge))

b1_id = sample_books[0][0]
b1_cid = sample_books[0][2]

order_items = [
    {"name": "Paneer Butter Masala", "quantity": 1, "unit_price": 320.0, "total": 320.0},
    {"name": "Butter Naan (2 pcs)", "quantity": 2, "unit_price": 90.0, "total": 180.0},
    {"name": "Fresh Mint Lime Soda", "quantity": 2, "unit_price": 120.0, "total": 240.0}
]

c.execute('''INSERT INTO food_orders (order_ref, booking_id, customer_id, items_json, total_amount, order_status, delivery_notes)
             VALUES ('ORD-2026-0001', ?, ?, ?, 740.0, 'Delivered', 'Delivered to room. Guest signed order slip.')''',
          (b1_id, b1_cid, json.dumps(order_items)))
ord_id = c.lastrowid

c.execute('''INSERT INTO expenses (expense_ref, booking_id, customer_id, category, description, quantity, unit_price, total_amount, expense_status, order_id)
             VALUES ('EXP-FO-0001', ?, ?, 'Room Service', 'Dinner Room Service (Paneer Butter Masala, Butter Naan, Mint Soda)', 1, 740.0, 740.0, 'Finalized', ?)''',
          (b1_id, b1_cid, ord_id))

c.execute('''INSERT INTO expenses (expense_ref, booking_id, customer_id, category, description, quantity, unit_price, total_amount, expense_status)
             VALUES ('EXP-LD-0001', ?, ?, 'Laundry', 'Express Pressing & Laundry (3 Garments)', 3, 100.0, 300.0, 'Finalized')''',
          (b1_id, b1_cid))

c.execute('''INSERT INTO payments (transaction_ref, booking_id, customer_id, amount, payment_method, transaction_status, notes)
             VALUES ('PAY-2026-0001', ?, ?, 3000.0, 'UPI', 'Success', 'Advance partial deposit paid via UPI')''',
          (b1_id, b1_cid))

c.execute('UPDATE bookings SET payment_status = \'Partially Paid\' WHERE id = ?', (b1_id,))

conn.commit()
conn.close()
print('Initial ledger expenses and payments seeded successfully!')

