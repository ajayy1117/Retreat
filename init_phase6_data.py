import sqlite3

conn = sqlite3.connect('retreat.db')
c = conn.cursor()

c.execute('SELECT COUNT(*) FROM physical_rooms')
count = c.fetchone()[0]

if count == 0:
    inventory = []
    # Floor 1: Comfort Room (Standard, id=1) -> 101-110
    for i in range(1, 11):
        inventory.append((f"{100+i}", 1, 1, 'Available', None, None))
    # Floor 2: Canopy Deluxe Room (Deluxe, id=2) -> 201-208
    for i in range(1, 9):
        inventory.append((f"{200+i}", 2, 2, 'Available', None, None))
    # Floor 3: Executive Suite (Executive Suite, id=3) -> 301-305
    for i in range(1, 6):
        inventory.append((f"{300+i}", 3, 3, 'Available', None, None))
    # Floor 4: Premium Family Suite (Premium Suite, id=4) -> 401-403
    for i in range(1, 4):
        inventory.append((f"{400+i}", 4, 4, 'Available', None, None))

    c.executemany('''
        INSERT INTO physical_rooms (room_number, room_category_id, floor, operational_status, current_booking_id, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', inventory)
    conn.commit()
    print(f"Populated {len(inventory)} physical rooms.")
else:
    print(f"physical_rooms already has {count} entries.")

# Sync booking 9 (Vikramaditya Singhania, Checked In to Executive Suite, Room 301)
c.execute("UPDATE physical_rooms SET operational_status = 'Occupied', current_booking_id = 9 WHERE room_number = '301'")
c.execute("UPDATE bookings SET assigned_room_number = '301', assigned_room_id = (SELECT id FROM physical_rooms WHERE room_number = '301'), actual_check_in = '2026-11-10 14:00:00' WHERE id = 9")

# Set Room 105 in Cleaning, Room 204 in Maintenance for realistic operations
c.execute("UPDATE physical_rooms SET operational_status = 'Cleaning', notes = 'Sanitization post checkout' WHERE room_number = '105'")
c.execute("UPDATE physical_rooms SET operational_status = 'Maintenance', notes = 'HVAC sensor calibration' WHERE room_number = '204'")

# Add sample service requests for booking 9 and others
c.execute("SELECT COUNT(*) FROM service_requests")
req_count = c.fetchone()[0]
if req_count == 0:
    samples = [
        ('REQ-2026-0001', 9, 'CUST-1001', '301', 'Housekeeping', 'Extra fresh bath linen and evening turndown', 'Standard', 'Completed', 0, 0.0, '2026-11-10 16:30:00', '2026-11-10 17:15:00', 'Completed by floor team', None),
        ('REQ-2026-0002', 9, 'CUST-1001', '301', 'Drinking Water', 'Two complimentary Himalayan mineral water bottles', 'High', 'Completed', 0, 0.0, '2026-11-10 18:00:00', '2026-11-10 18:15:00', 'Delivered to suite', None),
        ('REQ-2026-0003', 9, 'CUST-1001', '301', 'Laundry', 'Express dry cleaning: 2 formal shirts & 1 blazer', 'High', 'Pending', 1, 450.0, '2026-11-11 09:00:00', None, 'Scheduled pickup at 10 AM', None),
        ('REQ-2026-0004', 9, 'CUST-1001', '301', 'Maintenance assistance', 'Check balcony door lock mechanism', 'Medium', 'In Progress', 0, 0.0, '2026-11-11 11:30:00', None, 'Assigned to engineering technician', None)
    ]
    c.executemany('''
        INSERT INTO service_requests (request_ref, booking_id, customer_id, room_number, request_type, description, priority, status, is_chargeable, charge_amount, created_at, completed_at, notes, expense_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', samples)

conn.commit()
conn.close()
print("Data initialization complete.")

