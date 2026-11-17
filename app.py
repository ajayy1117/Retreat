import os
import random
import json
from functools import wraps
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from db import get_db

from booking_engine import (
    INVENTORY_DEFAULTS,
    MEAL_PLANS,
    HOTEL_POLICIES,
    format_inr,
    calculate_nights,
    validate_dates,
    calculate_availability_for_room,
    is_physical_room_available,
    calculate_pricing,
    calculate_cancellation_and_refund
)

from ledger_engine import (
    to_paise,
    from_paise,
    calculate_booking_financials,
    calculate_kpi_metrics
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'retreat_hospitality_prototype_secret_key_session')

def get_db_connection():
    return get_db()

def admin_required(f):
    """Protects admin endpoints: requires logged-in user with role == 'admin'."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        role = session.get('user_role')
        if role != 'admin':
            flash('Access restricted. Administrative credentials required.', 'error')
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def can_access_booking(booking_row):
    """
    Checks if currently active user can access booking:
    - Admin: always allowed.
    - Logged-in customer: customer_id or guest_email must match.
    - Not logged in (guest): allowed for prototype guest access.
    """
    if not booking_row:
        return False
    user_role = session.get('user_role')
    if user_role == 'admin':
        return True
    cust_id = session.get('customer_id')
    user_email = (session.get('user_email') or '').strip().lower()
    b_cust_id = booking_row['customer_id']
    b_email = (booking_row['guest_email'] or '').strip().lower()

    if cust_id:
        return (cust_id == b_cust_id) or (user_email and user_email == b_email)
    return True

def format_date_range(check_in_str, check_out_str):
    """Formats 2026-11-06 and 2026-11-09 into '06 Nov – 09 Nov 2026'."""
    try:
        ci = datetime.strptime(check_in_str, '%Y-%m-%d')
        co = datetime.strptime(check_out_str, '%Y-%m-%d')
        if ci.year == co.year:
            return f"{ci.strftime('%d %b')} – {co.strftime('%d %b %Y')}"
        return f"{ci.strftime('%d %b %Y')} – {co.strftime('%d %b %Y')}"
    except Exception:
        return f"{check_in_str} – {check_out_str}"

@app.template_filter('currency')
def currency_filter(value):
    return format_inr(value)

@app.template_filter('from_json')
def from_json_filter(value):
    try:
        return json.loads(value) if value else []
    except Exception:
        return []

@app.template_filter('date_range')
def date_range_filter(booking):
    if isinstance(booking, dict) or hasattr(booking, '__getitem__'):
        return format_date_range(booking['check_in'], booking['check_out'])
    return ""

@app.template_filter('format_date')
def format_date_filter(val):
    try:
        d = datetime.strptime(str(val)[:10], '%Y-%m-%d')
        return d.strftime('%d %b %Y')
    except Exception:
        return val

@app.template_filter('format_guest_count')
def format_guest_count_filter(adults, children=0):
    try:
        ad = int(adults or 0)
        ch = int(children or 0)
        ad_label = "1 Adult" if ad == 1 else f"{ad} Adults"
        ch_label = "1 Child" if ch == 1 else f"{ch} Children"
        return f"{ad_label} · {ch_label}"
    except Exception:
        return f"{adults} Adults · {children} Children"

@app.template_filter('format_date_span')
def format_date_span_filter(check_in, check_out):
    try:
        d1 = datetime.strptime(str(check_in)[:10], '%Y-%m-%d')
        d2 = datetime.strptime(str(check_out)[:10], '%Y-%m-%d')
        return f"{d1.strftime('%d %b %Y')} – {d2.strftime('%d %b %Y')}"
    except Exception:
        return f"{check_in} – {check_out}"

# Context processor so policies, meal plans, and current user session are accessible across all templates
@app.context_processor
def inject_global_data():
    current_user = None
    if 'customer_id' in session:
        conn = get_db_connection()
        user_row = conn.execute('SELECT customer_id, full_name, email, phone, role FROM customers WHERE customer_id = ?', (session['customer_id'],)).fetchone()
        conn.close()
        if user_row:
            current_user = dict(user_row)
    return {
        'HOTEL_POLICIES': HOTEL_POLICIES,
        'MEAL_PLANS': MEAL_PLANS,
        'current_user': current_user
    }

# ----------------- CUSTOMER AUTHENTICATION ROUTES ----------------- #

@app.route('/login', methods=['GET', 'POST'])
def login():
    redirect_target = request.args.get('next') or request.form.get('next') or ''
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = (request.form.get('password') or '').strip()

        if not email or not password:
            flash('Please enter both your email address and password.', 'error')
            return render_template('login.html', email=email, next=redirect_target)

        conn = get_db_connection()
        user = conn.execute('SELECT * FROM customers WHERE LOWER(email) = ?', (email,)).fetchone()
        conn.close()

        if not user or user['password'] != password:
            flash('Invalid email address or password. Please verify your credentials and try again.', 'error')
            return render_template('login.html', email=email, next=redirect_target)

        session['customer_id'] = user['customer_id']
        session['user_name'] = user['full_name']
        session['user_email'] = user['email']
        session['user_role'] = user['role']

        flash(f'Welcome back, {user["full_name"]}!', 'success')

        if user['role'] == 'admin':
            return redirect(url_for('admin_dashboard'))

        if redirect_target:
            return redirect(redirect_target)
        return redirect(url_for('my_bookings'))

    return render_template('login.html', next=redirect_target)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full_name = (request.form.get('full_name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        phone = (request.form.get('phone') or '').strip()
        password = (request.form.get('password') or '').strip()
        confirm_password = (request.form.get('confirm_password') or '').strip()

        if not full_name or not email or not phone or not password:
            flash('All fields are required. Please complete the form.', 'error')
            return render_template('register.html', full_name=full_name, email=email, phone=phone)

        if '@' not in email or '.' not in email:
            flash('Please enter a valid email address.', 'error')
            return render_template('register.html', full_name=full_name, email=email, phone=phone)

        if len(phone) < 8:
            flash('Please enter a valid phone number with country code.', 'error')
            return render_template('register.html', full_name=full_name, email=email, phone=phone)

        if len(password) < 6:
            flash('Password must be at least 6 characters long.', 'error')
            return render_template('register.html', full_name=full_name, email=email, phone=phone)

        if password != confirm_password:
            flash('Passwords do not match. Please re-enter carefully.', 'error')
            return render_template('register.html', full_name=full_name, email=email, phone=phone)

        conn = get_db_connection()
        existing = conn.execute('SELECT id FROM customers WHERE LOWER(email) = ?', (email,)).fetchone()
        if existing:
            conn.close()
            flash('An account with this email address already exists. Please login.', 'error')
            return redirect(url_for('login'))

        count_row = conn.execute('SELECT COUNT(*) FROM customers').fetchone()
        cust_id = f"CUST-{1000 + (count_row[0] or 0) + 1}"

        conn.execute('INSERT INTO customers (customer_id, full_name, email, phone, password, role) VALUES (?, ?, ?, ?, ?, ?)',
                     (cust_id, full_name, email, phone, password, 'customer'))
        conn.commit()
        conn.close()

        session['customer_id'] = cust_id
        session['user_name'] = full_name
        session['user_email'] = email
        session['user_role'] = 'customer'

        flash(f'Account created successfully! Welcome to RETREAT, {full_name}.', 'success')
        return redirect(url_for('my_bookings'))

    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out securely.', 'success')
    return redirect(url_for('home'))

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'customer_id' not in session:
        flash('Please log in to manage your customer profile.', 'error')
        return redirect(url_for('login', next=url_for('profile')))

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM customers WHERE customer_id = ?', (session['customer_id'],)).fetchone()

    if request.method == 'POST':
        full_name = (request.form.get('full_name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        phone = (request.form.get('phone') or '').strip()

        if not full_name or not email or not phone:
            flash('Full name, email, and phone cannot be empty.', 'error')
        else:
            conn.execute('UPDATE customers SET full_name = ?, email = ?, phone = ? WHERE customer_id = ?',
                         (full_name, email, phone, session['customer_id']))
            conn.commit()
            session['user_name'] = full_name
            session['user_email'] = email
            flash('Your profile details have been saved successfully.', 'success')
            user = conn.execute('SELECT * FROM customers WHERE customer_id = ?', (session['customer_id'],)).fetchone()

    conn.close()
    return render_template('profile.html', user=user)

# ----------------- CUSTOMER FACING ROUTES ----------------- #

@app.route('/')
def home():
    conn = get_db_connection()
    rooms = conn.execute('SELECT * FROM rooms WHERE is_available = 1 ORDER BY price_per_night ASC').fetchall()
    conn.close()
    
    today = date.today()
    default_check_in = (today + timedelta(days=1)).strftime('%Y-%m-%d')
    default_check_out = (today + timedelta(days=4)).strftime('%Y-%m-%d')
    
    return render_template(
        'index.html',
        rooms=rooms,
        default_check_in=default_check_in,
        default_check_out=default_check_out
    )

@app.route('/rooms')
def rooms_catalog():
    conn = get_db_connection()
    category = request.args.get('category')
    
    today = date.today()
    check_in = request.args.get('check_in') or (today + timedelta(days=1)).strftime('%Y-%m-%d')
    check_out = request.args.get('check_out') or (today + timedelta(days=4)).strftime('%Y-%m-%d')
    adults = int(request.args.get('adults') or 2)
    children = int(request.args.get('children') or 0)
    rooms_count = int(request.args.get('rooms_count') or 1)

    date_error = validate_dates(check_in, check_out)
    if date_error:
        flash(date_error, 'error')
        nights = 3
        check_in = (today + timedelta(days=1)).strftime('%Y-%m-%d')
        check_out = (today + timedelta(days=4)).strftime('%Y-%m-%d')
    else:
        nights = calculate_nights(check_in, check_out)

    reservations = conn.execute('SELECT * FROM bookings WHERE status NOT IN (\'Cancelled\', \'Completed\')').fetchall()
    res_list = [dict(r) for r in reservations]

    query = 'SELECT * FROM rooms WHERE is_available = 1'
    params = []
    if category and category != 'all':
        query += ' AND category = ?'
        params.append(category)
    query += ' ORDER BY price_per_night ASC'
    raw_rooms = conn.execute(query, params).fetchall()
    conn.close()

    rooms_with_availability = []
    for r in raw_rooms:
        r_dict = dict(r)
        avail = calculate_availability_for_room(
            room_id=r['id'],
            total_inventory=r['total_inventory'] or INVENTORY_DEFAULTS.get(r['id'], 5),
            check_in_str=check_in,
            check_out_str=check_out,
            active_reservations=res_list
        )
        r_dict['available_count'] = avail
        r_dict['is_sold_out'] = (avail < rooms_count)
        
        max_total_adults = (r['max_occupancy'] or r['capacity_adults']) * rooms_count
        r_dict['capacity_exceeded'] = (adults > max_total_adults)
        
        price_calc = calculate_pricing(
            base_price_per_night=r['price_per_night'],
            additional_guest_charge=r['additional_guest_charge'] or 0.0,
            capacity_adults=r['capacity_adults'],
            max_occupancy=r['max_occupancy'] or r['capacity_adults'],
            nights=nights,
            rooms_count=rooms_count,
            adults=adults,
            children=children
        )
        r_dict['pricing'] = price_calc
        rooms_with_availability.append(r_dict)

    return render_template(
        'rooms.html',
        rooms=rooms_with_availability,
        current_category=category,
        check_in=check_in,
        check_out=check_out,
        nights=nights,
        adults=adults,
        children=children,
        rooms_count=rooms_count,
        date_error=date_error
    )

@app.route('/rooms/<int:room_id>')
def room_detail(room_id):
    conn = get_db_connection()
    room = conn.execute('SELECT * FROM rooms WHERE id = ?', (room_id,)).fetchone()
    if not room:
        conn.close()
        return redirect(url_for('rooms_catalog'))

    today = date.today()
    check_in = request.args.get('check_in') or (today + timedelta(days=1)).strftime('%Y-%m-%d')
    check_out = request.args.get('check_out') or (today + timedelta(days=4)).strftime('%Y-%m-%d')
    adults = int(request.args.get('adults') or 2)
    children = int(request.args.get('children') or 0)
    rooms_count = int(request.args.get('rooms_count') or 1)

    date_error = validate_dates(check_in, check_out)
    nights = calculate_nights(check_in, check_out) if not date_error else 3

    reservations = conn.execute('SELECT * FROM bookings WHERE status NOT IN (\'Cancelled\', \'Completed\')').fetchall()
    res_list = [dict(r) for r in reservations]
    
    available_count = calculate_availability_for_room(
        room_id=room['id'],
        total_inventory=room['total_inventory'] or INVENTORY_DEFAULTS.get(room['id'], 5),
        check_in_str=check_in,
        check_out_str=check_out,
        active_reservations=res_list
    )

    similar_rooms = conn.execute('SELECT * FROM rooms WHERE id != ? AND is_available = 1 ORDER BY price_per_night ASC LIMIT 3', (room_id,)).fetchall()
    conn.close()

    pricing = calculate_pricing(
        base_price_per_night=room['price_per_night'],
        additional_guest_charge=room['additional_guest_charge'] or 0.0,
        capacity_adults=room['capacity_adults'],
        max_occupancy=room['max_occupancy'] or room['capacity_adults'],
        nights=nights,
        rooms_count=rooms_count,
        adults=adults,
        children=children
    )

    return render_template(
        'room_detail.html',
        room=room,
        available_count=available_count,
        similar_rooms=similar_rooms,
        check_in=check_in,
        check_out=check_out,
        nights=nights,
        adults=adults,
        children=children,
        rooms_count=rooms_count,
        pricing=pricing,
        policies=HOTEL_POLICIES
    )

@app.route('/book')
def booking_flow():
    room_id = request.args.get('room_id', type=int)
    if not room_id:
        return redirect(url_for('rooms_catalog'))

    conn = get_db_connection()
    room = conn.execute('SELECT * FROM rooms WHERE id = ?', (room_id,)).fetchone()
    all_rooms = conn.execute('SELECT id, name, category, price_per_night, max_occupancy FROM rooms WHERE is_available = 1').fetchall()
    
    logged_user = None
    if 'customer_id' in session:
        logged_user = conn.execute('SELECT * FROM customers WHERE customer_id = ?', (session['customer_id'],)).fetchone()
    conn.close()

    if not room:
        return redirect(url_for('rooms_catalog'))

    today = date.today()
    check_in = request.args.get('check_in') or (today + timedelta(days=1)).strftime('%Y-%m-%d')
    check_out = request.args.get('check_out') or (today + timedelta(days=4)).strftime('%Y-%m-%d')
    adults = int(request.args.get('adults') or 2)
    children = int(request.args.get('children') or 0)
    rooms_count = int(request.args.get('rooms_count') or 1)
    meal_plan_id = request.args.get('meal_plan') or 'room_only'

    return render_template(
        'book.html',
        room=room,
        all_rooms=all_rooms,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        children=children,
        rooms_count=rooms_count,
        selected_meal_plan=meal_plan_id,
        meal_plans=MEAL_PLANS,
        logged_user=logged_user
    )

@app.route('/api/calculate-price', methods=['POST'])
def api_calculate_price():
    data = request.get_json() or {}
    room_id = int(data.get('room_id') or 1)
    check_in = data.get('check_in')
    check_out = data.get('check_out')
    adults = int(data.get('adults') or 2)
    children = int(data.get('children') or 0)
    rooms_count = int(data.get('rooms_count') or 1)
    meal_plan_id = data.get('meal_plan') or 'room_only'

    date_err = validate_dates(check_in, check_out)
    if date_err:
        return jsonify({'status': 'error', 'message': date_err}), 400

    nights = calculate_nights(check_in, check_out)

    conn = get_db_connection()
    room = conn.execute('SELECT * FROM rooms WHERE id = ?', (room_id,)).fetchone()
    reservations = conn.execute('SELECT * FROM bookings WHERE status NOT IN (\'Cancelled\', \'Completed\')').fetchall()
    conn.close()

    if not room:
        return jsonify({'status': 'error', 'message': 'Selected room does not exist.'}), 404

    max_capacity = (room['max_occupancy'] or room['capacity_adults']) * rooms_count
    if adults > max_capacity:
        return jsonify({
            'status': 'error',
            'code': 'CAPACITY_EXCEEDED',
            'message': f"Maximum capacity for {rooms_count} {room['name']}(s) is {max_capacity} guests. Please book an additional room or select a larger suite."
        }), 400

    res_list = [dict(r) for r in reservations]
    available_count = calculate_availability_for_room(
        room_id=room_id,
        total_inventory=room['total_inventory'] or INVENTORY_DEFAULTS.get(room_id, 5),
        check_in_str=check_in,
        check_out_str=check_out,
        active_reservations=res_list
    )

    if available_count < rooms_count:
        return jsonify({
            'status': 'error',
            'code': 'ROOM_UNAVAILABLE',
            'available_count': available_count,
            'message': f"Only {available_count} {room['name']}(s) available for the selected dates ({check_in} to {check_out}). Please adjust your dates or room count."
        }), 400

    pricing = calculate_pricing(
        base_price_per_night=room['price_per_night'],
        additional_guest_charge=room['additional_guest_charge'] or 0.0,
        capacity_adults=room['capacity_adults'],
        max_occupancy=room['max_occupancy'] or room['capacity_adults'],
        nights=nights,
        rooms_count=rooms_count,
        adults=adults,
        children=children,
        meal_plan_id=meal_plan_id
    )

    return jsonify({
        'status': 'success',
        'available_count': available_count,
        'pricing': pricing
    })

@app.route('/api/confirm-booking', methods=['POST'])
def api_confirm_booking():
    data = request.get_json() or {}
    room_id = int(data.get('room_id') or 1)
    check_in = data.get('check_in')
    check_out = data.get('check_out')
    adults = int(data.get('adults') or 2)
    children = int(data.get('children') or 0)
    rooms_count = int(data.get('rooms_count') or 1)
    meal_plan_id = data.get('meal_plan') or 'room_only'
    
    guest_name = data.get('guest_name', '').strip()
    guest_email = data.get('guest_email', '').strip()
    guest_phone = data.get('guest_phone', '').strip()
    special_requests = data.get('special_requests', '').strip()

    if not guest_name or not guest_email:
        return jsonify({'status': 'error', 'message': 'Guest name and valid email are required.'}), 400

    date_err = validate_dates(check_in, check_out)
    if date_err:
        return jsonify({'status': 'error', 'message': date_err}), 400

    nights = calculate_nights(check_in, check_out)

    conn = get_db_connection()
    room = conn.execute('SELECT * FROM rooms WHERE id = ?', (room_id,)).fetchone()
    if not room:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Selected room not found.'}), 404

    customer_id = session.get('customer_id')
    if not customer_id:
        existing_cust = conn.execute('SELECT customer_id FROM customers WHERE LOWER(email) = ?', (guest_email.lower(),)).fetchone()
        if existing_cust:
            customer_id = existing_cust['customer_id']
        else:
            count_cust = conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
            customer_id = f"CUST-{1000 + count_cust + 1}"
            conn.execute('INSERT INTO customers (customer_id, full_name, email, phone, password, role) VALUES (?, ?, ?, ?, ?, ?)',
                         (customer_id, guest_name, guest_email.lower(), guest_phone, 'guest123', 'customer'))
        session['customer_id'] = customer_id
        session['user_name'] = guest_name
        session['user_email'] = guest_email
        session['user_role'] = 'customer'

    reservations = conn.execute('SELECT * FROM bookings WHERE status NOT IN (\'Cancelled\', \'Completed\')').fetchall()
    res_list = [dict(r) for r in reservations]
    available_count = calculate_availability_for_room(
        room_id=room_id,
        total_inventory=room['total_inventory'] or INVENTORY_DEFAULTS.get(room_id, 5),
        check_in_str=check_in,
        check_out_str=check_out,
        active_reservations=res_list
    )

    if available_count < rooms_count:
        conn.close()
        return jsonify({
            'status': 'error',
            'code': 'ROOM_UNAVAILABLE',
            'message': f"Apologies, {room['name']} is no longer available in the requested quantity for these dates."
        }), 409

    pricing = calculate_pricing(
        base_price_per_night=room['price_per_night'],
        additional_guest_charge=room['additional_guest_charge'] or 0.0,
        capacity_adults=room['capacity_adults'],
        max_occupancy=room['max_occupancy'] or room['capacity_adults'],
        nights=nights,
        rooms_count=rooms_count,
        adults=adults,
        children=children,
        meal_plan_id=meal_plan_id
    )

    # Generate a collision-free booking code
    max_id_row = conn.execute('SELECT MAX(id) FROM bookings').fetchone()
    seq_num = (max_id_row[0] or 0) + 1
    booking_code = f"RET-2026-{seq_num:04d}"
    # Verify uniqueness in case of out-of-order sequences
    while conn.execute('SELECT id FROM bookings WHERE booking_code = ?', (booking_code,)).fetchone():
        seq_num += 1
        booking_code = f"RET-2026-{seq_num:04d}"

    c_cur = conn.cursor()
    c_cur.execute('''INSERT INTO bookings 
        (booking_code, customer_id, room_id, room_name, guest_name, guest_email, guest_phone, check_in, check_out, 
         guests_adults, guests_children, rooms_count, total_nights, total_price, base_room_charges, 
         extra_guest_charges, meal_plan, meal_plan_charge, discount_amount, special_requests, status, payment_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            booking_code,
            customer_id,
            room['id'],
            room['name'],
            guest_name,
            guest_email,
            guest_phone,
            check_in,
            check_out,
            adults,
            children,
            rooms_count,
            nights,
            pricing['total_amount'],
            pricing['base_room_charges'],
            pricing['additional_guest_charges'],
            pricing['meal_plan_name'],
            pricing['meal_plan_charges'],
            pricing['discount_amount'],
            special_requests,
            'Confirmed',
            'Unpaid'
        )
    )
    new_booking_id = c_cur.lastrowid

    # Automatically create initial Room Charges and Meal Plan ledger expense entries
    base_charge_total = pricing['base_room_charges'] + pricing['additional_guest_charges']
    c_cur.execute('''INSERT INTO expenses (expense_ref, booking_id, customer_id, category, description, quantity, unit_price, total_amount, expense_status)
                     VALUES (?, ?, ?, 'Room Charges', ?, 1, ?, ?, 'Finalized')''',
                  (f"EXP-RC-{new_booking_id:04d}", new_booking_id, customer_id, f"Base Room Accommodation ({nights} nights, {rooms_count} room)", base_charge_total, base_charge_total))

    if pricing['meal_plan_charges'] > 0:
        c_cur.execute('''INSERT INTO expenses (expense_ref, booking_id, customer_id, category, description, quantity, unit_price, total_amount, expense_status)
                         VALUES (?, ?, ?, 'Meal Plan', ?, 1, ?, ?, 'Finalized')''',
                      (f"EXP-MP-{new_booking_id:04d}", new_booking_id, customer_id, f"Meal Plan ({pricing['meal_plan_name']}) Package", pricing['meal_plan_charges'], pricing['meal_plan_charges']))

    conn.commit()
    conn.close()

    return jsonify({
        'status': 'success',
        'booking_code': booking_code,
        'customer_id': customer_id,
        'redirect_url': url_for('booking_confirmation', code=booking_code)
    })

@app.route('/booking/confirmation')
def booking_confirmation():
    code = request.args.get('code')
    if not code:
        return redirect(url_for('home'))

    conn = get_db_connection()
    booking = conn.execute('SELECT b.*, r.image_url, r.size_sqft, r.bed_type FROM bookings b LEFT JOIN rooms r ON b.room_id = r.id WHERE b.booking_code = ?', (code,)).fetchone()
    
    if not booking:
        conn.close()
        flash('Booking reference not found.', 'error')
        return redirect(url_for('home'))

    # Retrieve expenses & payments for this booking
    expenses = conn.execute('SELECT * FROM expenses WHERE booking_id = ? ORDER BY created_at ASC', (booking['id'],)).fetchall()
    payments = conn.execute('SELECT * FROM payments WHERE booking_id = ? ORDER BY transaction_date ASC', (booking['id'],)).fetchall()
    conn.close()

    fin = calculate_booking_financials(dict(booking), [dict(e) for e in expenses], [dict(p) for p in payments])

    return render_template('booking_confirmation.html', booking=booking, financials=fin, payments=payments)

@app.route('/my-bookings')
def my_bookings():
    conn = get_db_connection()
    logged_in_id = session.get('customer_id')
    user_email = session.get('user_email', '')

    if logged_in_id:
        bookings_raw = conn.execute('''SELECT b.*, r.image_url FROM bookings b 
                                   LEFT JOIN rooms r ON b.room_id = r.id 
                                   WHERE b.customer_id = ? OR LOWER(b.guest_email) = LOWER(?)
                                   ORDER BY b.created_at DESC''', (logged_in_id, user_email)).fetchall()
    else:
        email_query = request.args.get('email', '').strip()
        if email_query:
            bookings_raw = conn.execute('SELECT b.*, r.image_url FROM bookings b LEFT JOIN rooms r ON b.room_id = r.id WHERE LOWER(b.guest_email) = LOWER(?) ORDER BY b.created_at DESC', (email_query,)).fetchall()
        else:
            bookings_raw = []

    # Calculate real-time financials for each booking in list
    bookings_with_fin = []
    for b in bookings_raw:
        b_dict = dict(b)
        exps = conn.execute('SELECT * FROM expenses WHERE booking_id = ?', (b['id'],)).fetchall()
        pmts = conn.execute('SELECT * FROM payments WHERE booking_id = ?', (b['id'],)).fetchall()
        fin = calculate_booking_financials(b_dict, [dict(e) for e in exps], [dict(p) for p in pmts])
        b_dict['financials'] = fin
        bookings_with_fin.append(b_dict)

    all_mock_customers = conn.execute('SELECT customer_id, full_name, email, role FROM customers').fetchall()
    conn.close()

    return render_template('my_bookings.html', bookings=bookings_with_fin, all_mock_customers=all_mock_customers)

@app.route('/switch-user/<cust_id>')
def switch_user(cust_id):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM customers WHERE customer_id = ?', (cust_id,)).fetchone()
    conn.close()
    if user:
        session['customer_id'] = user['customer_id']
        session['user_name'] = user['full_name']
        session['user_email'] = user['email']
        session['user_role'] = user['role']
        flash(f'Switched session to {user["full_name"]} ({user["role"]}).', 'success')
        if user['role'] == 'admin':
            return redirect(url_for('admin_dashboard'))
    return redirect(url_for('my_bookings'))

@app.route('/my-bookings/<int:booking_id>/cancel', methods=['POST'])
def cancel_customer_booking(booking_id):
    conn = get_db_connection()
    booking = conn.execute('SELECT * FROM bookings WHERE id = ?', (booking_id,)).fetchone()
    if not booking:
        conn.close()
        flash('Reservation not found.', 'error')
        return redirect(url_for('my_bookings'))

    if booking['status'] in ['Cancelled', 'Completed', 'Checked Out']:
        conn.close()
        flash(f"Cannot cancel a reservation with status '{booking['status']}'.", 'error')
        return redirect(url_for('booking_details', booking_id=booking_id))

    # 1. Gather all payments, expenses, food orders, and service requests
    payments = [dict(p) for p in conn.execute('SELECT * FROM payments WHERE booking_id = ?', (booking_id,)).fetchall()]
    total_paid = sum(float(p.get('amount') or 0.0) for p in payments if p.get('transaction_status') in ['Success', 'Completed', 'Paid', None])
    
    # Check completed billable incidental expenses (food delivered, completed paid service requests, EXCLUDING base room charges)
    completed_expenses = [dict(e) for e in conn.execute("SELECT * FROM expenses WHERE booking_id = ? AND expense_status = 'Finalized' AND category != 'Room Charges'", (booking_id,)).fetchall()]
    completed_services_charge = sum(float(e.get('total_amount') or 0.0) for e in completed_expenses)

    # Check previous refunds
    existing_refunds = [dict(r) for r in conn.execute("SELECT * FROM refunds WHERE booking_id = ? AND refund_status = 'Refunded'", (booking_id,)).fetchall()]
    previous_refunds = sum(float(r.get('refund_amount') or 0.0) for r in existing_refunds)

    # 2. Centralized refund calculation
    refund_calc = calculate_cancellation_and_refund(
        booking=dict(booking),
        total_paid=total_paid,
        completed_services_charge=completed_services_charge,
        previous_refunds=previous_refunds
    )

    # 3. Release physical room if assigned and not occupied
    if booking['assigned_room_number']:
        conn.execute("UPDATE physical_rooms SET operational_status = 'Available', current_booking_id = NULL WHERE room_number = ?", 
                     (booking['assigned_room_number'],))

    # 4. Cancel pending future food orders
    conn.execute("UPDATE food_orders SET order_status = 'Cancelled' WHERE booking_id = ? AND order_status NOT IN ('Delivered', 'Cancelled')", 
                 (booking_id,))

    # 5. Cancel pending future service requests
    conn.execute("UPDATE service_requests SET status = 'Cancelled' WHERE booking_id = ? AND status NOT IN ('Completed', 'Cancelled')", 
                 (booking_id,))

    # 6. Mark booking as Cancelled
    conn.execute("UPDATE bookings SET status = 'Cancelled', assigned_room_id = NULL, assigned_room_number = NULL WHERE id = ?", (booking_id,))

    # 7. Record refund transaction if refundable_amount > 0
    refund_amount = refund_calc['refundable_amount']
    if refund_amount > 0:
        count_refunds = conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
        refund_ref = f"REF-2026-{count_refunds + 1:04d}"
        conn.execute('''INSERT INTO refunds (refund_ref, booking_id, customer_id, refund_amount, total_paid, eligible_charges, cancellation_penalty, reason, refund_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Refunded')''',
                     (refund_ref, booking_id, booking['customer_id'], refund_amount, total_paid, 
                      refund_calc['total_eligible_charges'], refund_calc['cancellation_penalty'], refund_calc['policy_description']))
        
        # Record offsetting payment entry for refund tracking
        count_payments = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        tx_ref = f"TXN-REF-{count_payments + 1:04d}"
        conn.execute('''INSERT INTO payments (transaction_ref, booking_id, customer_id, amount, payment_method, transaction_status, notes)
                        VALUES (?, ?, ?, ?, 'Simulated Refund', 'Refunded', ?)''',
                     (tx_ref, booking_id, booking['customer_id'], -refund_amount, f"Refund {refund_ref}: {refund_calc['policy_description']}"))

    conn.commit()
    conn.close()

    if refund_amount > 0:
        flash(f"Reservation {booking['booking_code']} has been cancelled. Simulated refund of {format_inr(refund_amount)} processed ({refund_calc['policy_description']}).", 'success')
    else:
        flash(f"Reservation {booking['booking_code']} has been cancelled. Room inventory released.", 'success')

    return redirect(url_for('booking_details', booking_id=booking_id))

# ----------------- PHASE 5: PAYMENTS & FINANCIAL MANAGEMENT ----------------- #

@app.route('/my-bookings/<int:booking_id>')
def booking_details(booking_id):
    """Dedicated detailed reservation management page for customers."""
    conn = get_db_connection()
    booking = conn.execute('''SELECT b.*, r.image_url, r.category as room_category, r.size_sqft, r.bed_type, r.view as room_view 
                              FROM bookings b 
                              LEFT JOIN rooms r ON b.room_id = r.id 
                              WHERE b.id = ?''', (booking_id,)).fetchone()
    if not booking:
        conn.close()
        flash('Reservation record not found.', 'error')
        return redirect(url_for('my_bookings'))

    if not can_access_booking(booking):
        conn.close()
        flash('Access restricted. You are not authorized to view this reservation.', 'error')
        return redirect(url_for('my_bookings'))

    expenses = conn.execute('SELECT * FROM expenses WHERE booking_id = ? ORDER BY created_at ASC', (booking_id,)).fetchall()
    payments = conn.execute('SELECT * FROM payments WHERE booking_id = ? ORDER BY transaction_date ASC', (booking_id,)).fetchall()
    orders = conn.execute('SELECT * FROM food_orders WHERE booking_id = ? ORDER BY order_date DESC', (booking_id,)).fetchall()
    service_requests = conn.execute('SELECT * FROM service_requests WHERE booking_id = ? ORDER BY created_at DESC', (booking_id,)).fetchall()
    invoice = conn.execute('SELECT * FROM invoices WHERE booking_id = ?', (booking_id,)).fetchone()
    refunds = conn.execute('SELECT * FROM refunds WHERE booking_id = ? ORDER BY created_at DESC', (booking_id,)).fetchall()

    # Pre-calculate cancellation & refund info preview
    total_paid = sum(float(p['amount'] or 0.0) for p in payments if p['transaction_status'] in ['Success', 'Completed', 'Paid', None])
    completed_expenses = [dict(e) for e in expenses if e['expense_status'] == 'Finalized' and e['category'] != 'Room Charges']
    completed_services_charge = sum(float(e['total_amount'] or 0.0) for e in completed_expenses)
    previous_refunds = sum(float(r['refund_amount'] or 0.0) for r in refunds if r['refund_status'] == 'Refunded')

    refund_preview = calculate_cancellation_and_refund(
        booking=dict(booking),
        total_paid=total_paid,
        completed_services_charge=completed_services_charge,
        previous_refunds=previous_refunds
    )

    conn.close()

    fin = calculate_booking_financials(dict(booking), [dict(e) for e in expenses], [dict(p) for p in payments])
    finalized_ledger_items = fin['finalized_expenses']

    # Assign realistic room number based on booking id
    room_number = booking['assigned_room_number'] or f"{300 + (booking['id'] % 20):03d}"

    return render_template('booking_details.html', 
                           booking=booking, 
                           room_number=room_number,
                           financials=fin, 
                           expenses=finalized_ledger_items, 
                           payments=payments, 
                           orders=orders,
                           service_requests=service_requests,
                           invoice=invoice,
                           refunds=refunds,
                           refund_preview=refund_preview)

@app.route('/bookings/<int:booking_id>/payments', methods=['GET', 'POST'])
def booking_payments(booking_id):
    """Payment management view for a specific reservation (Full & Partial payments)."""
    conn = get_db_connection()
    booking = conn.execute('SELECT b.*, r.image_url FROM bookings b LEFT JOIN rooms r ON b.room_id = r.id WHERE b.id = ?', (booking_id,)) .fetchone()
    if not booking:
        conn.close()
        flash('Booking not found.', 'error')
        return redirect(url_for('my_bookings'))

    if not can_access_booking(booking):
        conn.close()
        flash('Access restricted. You are not authorized to access this checkout settlement.', 'error')
        return redirect(url_for('my_bookings'))

    if request.method == 'POST':
        # Process simulated payment
        payment_type = request.form.get('payment_type', 'full')
        amount_input = request.form.get('amount')
        payment_method = request.form.get('payment_method', 'UPI')
        notes = request.form.get('notes', '').strip()

        # Fetch current ledger & payments
        expenses = conn.execute('SELECT * FROM expenses WHERE booking_id = ?', (booking_id,)).fetchall()
        payments = conn.execute('SELECT * FROM payments WHERE booking_id = ?', (booking_id,)).fetchall()
        fin = calculate_booking_financials(dict(booking), [dict(e) for e in expenses], [dict(p) for p in payments])
        
        rem_balance = fin['outstanding_balance']

        if rem_balance <= 0:
            flash('This reservation is already fully settled. No additional payment required.', 'error')
            conn.close()
            return redirect(url_for('booking_payments', booking_id=booking_id))

        if payment_type == 'full':
            pay_amount = rem_balance
        else:
            try:
                pay_amount = float(amount_input)
            except (ValueError, TypeError):
                flash('Please enter a valid partial payment amount.', 'error')
                conn.close()
                return redirect(url_for('booking_payments', booking_id=booking_id))

        # Validations
        if pay_amount <= 0:
            flash('Payment amount must be greater than ₹0.', 'error')
            conn.close()
            return redirect(url_for('booking_payments', booking_id=booking_id))

        if pay_amount > rem_balance:
            flash(f"Payment cannot exceed the remaining balance of {format_inr(rem_balance)}.", 'error')
            conn.close()
            return redirect(url_for('booking_payments', booking_id=booking_id))

        # Generate unique transaction reference PAY-2026-XXXX
        count_pmt = conn.execute('SELECT COUNT(*) FROM payments').fetchone()[0]
        tx_ref = f"PAY-2026-{count_pmt + 1:04d}"

        conn.execute('''INSERT INTO payments (transaction_ref, booking_id, customer_id, amount, payment_method, transaction_status, notes)
                        VALUES (?, ?, ?, ?, ?, 'Success', ?)''',
                     (tx_ref, booking_id, booking['customer_id'], pay_amount, payment_method, notes or f"{payment_type.capitalize()} Payment via {payment_method}"))
        
        # Re-evaluate financials to update booking payment_status
        new_payments = conn.execute('SELECT * FROM payments WHERE booking_id = ?', (booking_id,)).fetchall()
        new_fin = calculate_booking_financials(dict(booking), [dict(e) for e in expenses], [dict(p) for p in new_payments])

        conn.execute('UPDATE bookings SET payment_status = ? WHERE id = ?', (new_fin['payment_status'], booking_id))
        conn.commit()
        conn.close()

        flash(f"Payment of {format_inr(pay_amount)} via {payment_method} was successful! Ref: {tx_ref}", 'success')
        return redirect(url_for('booking_payments', booking_id=booking_id))

    # GET: Retrieve full ledger & history
    expenses = conn.execute('SELECT * FROM expenses WHERE booking_id = ? ORDER BY created_at ASC', (booking_id,)).fetchall()
    payments = conn.execute('SELECT * FROM payments WHERE booking_id = ? ORDER BY transaction_date DESC', (booking_id,)).fetchall()
    conn.close()

    fin = calculate_booking_financials(dict(booking), [dict(e) for e in expenses], [dict(p) for p in payments])

    return render_template('payments.html', booking=booking, financials=fin, payments=payments)

# ----------------- PHASE 5: EXPENSE LEDGER & CUSTOMER DASHBOARD ----------------- #

@app.route('/bookings/<int:booking_id>/ledger')
def customer_ledger(booking_id):
    """Detailed itemized expense ledger for customer booking."""
    conn = get_db_connection()
    booking = conn.execute('SELECT b.*, r.image_url FROM bookings b LEFT JOIN rooms r ON b.room_id = r.id WHERE b.id = ?', (booking_id,)).fetchone()
    if not booking:
        conn.close()
        flash('Booking record not found.', 'error')
        return redirect(url_for('my_bookings'))

    if not can_access_booking(booking):
        conn.close()
        flash('Access restricted. You are not authorized to view this ledger statement.', 'error')
        return redirect(url_for('my_bookings'))

    expenses = conn.execute('SELECT * FROM expenses WHERE booking_id = ? ORDER BY created_at ASC', (booking_id,)).fetchall()
    payments = conn.execute('SELECT * FROM payments WHERE booking_id = ? ORDER BY transaction_date ASC', (booking_id,)).fetchall()
    orders = conn.execute('SELECT * FROM food_orders WHERE booking_id = ? ORDER BY order_date DESC', (booking_id,)).fetchall()
    conn.close()

    fin = calculate_booking_financials(dict(booking), [dict(e) for e in expenses], [dict(p) for p in payments])
    finalized_ledger_items = fin['finalized_expenses']

    return render_template('ledger.html', 
                           booking=booking, 
                           expenses=finalized_ledger_items, 
                           payments=payments, 
                           orders=orders, 
                           financials=fin)

# ----------------- PHASE 5: GUEST SERVICES & ROOM SERVICE ORDERS ----------------- #

@app.route('/guest-services')
def guest_services():
    """Consolidated Guest Services landing page featuring Room Service and Other Guest Services."""
    conn = get_db_connection()
    cust_id = session.get('customer_id')
    user_email = session.get('user_email', '')
    active_bookings = []

    if cust_id:
        active_bookings = conn.execute('''SELECT id, booking_code, room_name, assigned_room_number, guest_name, status, check_in, check_out 
                                          FROM bookings 
                                          WHERE (customer_id = ? OR LOWER(guest_email) = LOWER(?)) 
                                            AND status IN ('Confirmed', 'Checked In')
                                          ORDER BY check_in ASC''', (cust_id, user_email)).fetchall()
        # Retrieve user's existing service requests across their active or past stays
        existing_requests = conn.execute('''SELECT s.*, b.booking_code, b.room_name 
                                             FROM service_requests s
                                             JOIN bookings b ON s.booking_id = b.id
                                             WHERE (b.customer_id = ? OR LOWER(b.guest_email) = LOWER(?))
                                             ORDER BY s.id DESC LIMIT 10''', (cust_id, user_email)).fetchall()
    else:
        # If not logged in, provide active in-house bookings and recent prototype service requests
        active_bookings = conn.execute('''SELECT id, booking_code, room_name, assigned_room_number, guest_name, status, check_in, check_out 
                                          FROM bookings 
                                          WHERE status IN ('Confirmed', 'Checked In')
                                          ORDER BY check_in ASC LIMIT 5''').fetchall()
        existing_requests = conn.execute('''SELECT s.*, b.booking_code, b.room_name 
                                             FROM service_requests s
                                             JOIN bookings b ON s.booking_id = b.id
                                             ORDER BY s.id DESC LIMIT 6''').fetchall()

    conn.close()
    return render_template('guest_services.html', active_bookings=active_bookings, existing_requests=existing_requests)

@app.route('/dining/menu')
def food_menu():
    """Customer-facing food & room service menu."""
    category = request.args.get('category')
    booking_id = request.args.get('booking_id', type=int)

    conn = get_db_connection()
    
    query = 'SELECT * FROM menu_items WHERE is_available = 1'
    params = []
    if category:
        query += ' AND category = ?'
        params.append(category)
    query += ' ORDER BY category ASC, price ASC'
    
    menu_items = conn.execute(query, params).fetchall()
    categories = [r[0] for r in conn.execute('SELECT DISTINCT category FROM menu_items').fetchall()]
    
    # Active bookings of the logged in user or general active bookings for ordering
    cust_id = session.get('customer_id')
    user_email = session.get('user_email', '')
    active_bookings = []
    if cust_id:
        active_bookings = conn.execute('''SELECT id, booking_code, room_name, customer_id, guest_name 
                                          FROM bookings 
                                          WHERE (customer_id = ? OR LOWER(guest_email) = LOWER(?)) 
                                            AND status NOT IN ('Cancelled', 'Checked Out')''', 
                                       (cust_id, user_email)).fetchall()
    else:
        active_bookings = conn.execute('SELECT id, booking_code, room_name, customer_id, guest_name FROM bookings WHERE status NOT IN (\'Cancelled\', \'Checked Out\') LIMIT 5').fetchall()

    # Preselected booking verification
    preselected_booking = None
    if booking_id:
        preselected_booking = conn.execute('SELECT * FROM bookings WHERE id = ?', (booking_id,)).fetchone()

    conn.close()
    return render_template('dining_menu.html', 
                           menu_items=menu_items, 
                           categories=categories, 
                           current_cat=category, 
                           active_bookings=active_bookings, 
                           preselected_bid=booking_id,
                           preselected_booking=preselected_booking)

@app.route('/api/order-food', methods=['POST'])
def api_order_food():
    """Place mock room service order."""
    data = request.get_json() or {}
    booking_id = int(data.get('booking_id') or 0)
    items = data.get('items', [])
    notes = data.get('delivery_notes', '').strip()

    if not booking_id or not items:
        return jsonify({'status': 'error', 'message': 'Active reservation and at least 1 menu item required.'}), 400

    conn = get_db_connection()
    booking = conn.execute('SELECT * FROM bookings WHERE id = ?', (booking_id,)).fetchone()
    if not booking:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Reservation not found.'}), 404

    # Calculate total and validate items against DB
    total_amount = 0.0
    validated_items = []
    for itm in items:
        m_id = itm.get('id')
        qty = int(itm.get('qty', 1))
        menu_row = conn.execute('SELECT * FROM menu_items WHERE id = ?', (m_id,)).fetchone()
        if menu_row and qty > 0:
            line_tot = menu_row['price'] * qty
            total_amount += line_tot
            validated_items.append({
                'id': menu_row['id'],
                'name': menu_row['name'],
                'category': menu_row['category'],
                'quantity': qty,
                'unit_price': menu_row['price'],
                'total': line_tot
            })

    if not validated_items:
        conn.close()
        return jsonify({'status': 'error', 'message': 'No valid menu items selected.'}), 400

    count_ord = conn.execute('SELECT COUNT(*) FROM food_orders').fetchone()[0]
    order_ref = f"ORD-2026-{count_ord + 1:04d}"
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    timeline_init = {
        'Placed': now_str
    }

    conn.execute('''INSERT INTO food_orders (order_ref, booking_id, customer_id, items_json, total_amount, order_status, delivery_notes, status_timeline_json)
                    VALUES (?, ?, ?, ?, ?, 'Placed', ?, ?)''',
                 (order_ref, booking_id, booking['customer_id'], json.dumps(validated_items), total_amount, notes, json.dumps(timeline_init)))
    conn.commit()
    conn.close()

    return jsonify({
        'status': 'success',
        'order_ref': order_ref,
        'total_amount': format_inr(total_amount),
        'message': f"Order {order_ref} placed successfully with status 'Placed'. It will appear as pending until marked 'Delivered' by staff."
    })

# ----------------- CUSTOMER SERVICE REQUESTS ----------------- #

@app.route('/api/service-requests', methods=['POST'])
def api_create_service_request():
    """Allows guests/customers to request room services: Housekeeping, Extra Towels, Laundry, Maintenance, etc."""
    data = request.get_json() or {}
    booking_id = int(data.get('booking_id') or 0)
    req_type = data.get('request_type', 'Housekeeping').strip()
    description = data.get('description', '').strip()
    priority = data.get('priority', 'Standard').strip()

    if not booking_id or not description:
        return jsonify({'status': 'error', 'message': 'Reservation and description are required.'}), 400

    conn = get_db_connection()
    booking = conn.execute('SELECT * FROM bookings WHERE id = ?', (booking_id,)).fetchone()
    if not booking:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Reservation record not found.'}), 404

    if not can_access_booking(booking):
        conn.close()
        return jsonify({'status': 'error', 'message': 'Not authorized to request service for this booking.'}), 403

    if booking['status'] not in ['Confirmed', 'Checked In']:
        conn.close()
        return jsonify({'status': 'error', 'message': f"Service requests can only be submitted for active in-house stays (current status: {booking['status']})."}), 400

    # Service catalog charges (complimentary vs billable)
    chargeable_catalog = {
        'Laundry': 350.0,
        'Dry Cleaning': 600.0,
        'Extra Bedding': 500.0,
        'Airport Transfer': 1200.0
    }
    is_chargeable = 1 if req_type in chargeable_catalog else 0
    charge_amount = chargeable_catalog.get(req_type, 0.0)

    count_req = conn.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0]
    req_ref = f"REQ-2026-{count_req + 1:04d}"
    room_num = booking['assigned_room_number'] or f"{300 + (booking['id'] % 20):03d}"

    conn.execute('''INSERT INTO service_requests (request_ref, booking_id, customer_id, room_number, request_type, description, priority, status, is_chargeable, charge_amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'Requested', ?, ?)''',
                 (req_ref, booking_id, booking['customer_id'], room_num, req_type, description, priority, is_chargeable, charge_amount))
    conn.commit()
    conn.close()

    charge_msg = f" (Billable: {format_inr(charge_amount)} upon completion)" if is_chargeable else " (Complimentary)"
    return jsonify({
        'status': 'success',
        'request_ref': req_ref,
        'request_type': req_type,
        'is_chargeable': bool(is_chargeable),
        'charge_amount': charge_amount,
        'charge_amount_fmt': format_inr(charge_amount),
        'message': f"Service request {req_ref} ({req_type}) submitted successfully{charge_msg}."
    })

# ----------------- PHASE 5: CHECKOUT BILLING & INVOICING ----------------- #

@app.route('/bookings/<int:booking_id>/checkout', methods=['GET', 'POST'])
def checkout_billing(booking_id):
    """Checkout billing and settlement workflow."""
    conn = get_db_connection()
    booking = conn.execute('SELECT b.*, r.image_url, r.bed_type, r.size_sqft FROM bookings b LEFT JOIN rooms r ON b.room_id = r.id WHERE b.id = ?', (booking_id,)).fetchone()
    if not booking:
        conn.close()
        flash('Booking not found.', 'error')
        return redirect(url_for('my_bookings'))

    if not can_access_booking(booking):
        conn.close()
        flash('Access restricted. You are not authorized to access this checkout settlement.', 'error')
        return redirect(url_for('my_bookings'))

    # If already checked out, redirect to invoice
    if booking['status'] == 'Checked Out':
        conn.close()
        return redirect(url_for('view_invoice', booking_id=booking_id))

    expenses = conn.execute('SELECT * FROM expenses WHERE booking_id = ? ORDER BY created_at ASC', (booking_id,)).fetchall()
    payments = conn.execute('SELECT * FROM payments WHERE booking_id = ? ORDER BY transaction_date ASC', (booking_id,)).fetchall()
    pending_orders = conn.execute('SELECT * FROM food_orders WHERE booking_id = ? AND order_status NOT IN (\'Delivered\', \'Cancelled\')', (booking_id,)).fetchall()

    fin = calculate_booking_financials(dict(booking), [dict(e) for e in expenses], [dict(p) for p in payments])
    finalized_ledger_items = fin['finalized_expenses']

    if request.method == 'POST':
        action = request.form.get('action')

        # Scenario 1: Pay remaining balance at checkout
        if action == 'pay_checkout':
            pay_method = request.form.get('payment_method', 'Credit/Debit Card')
            amount_to_pay = fin['outstanding_balance']

            if amount_to_pay > 0:
                count_pmt = conn.execute('SELECT COUNT(*) FROM payments').fetchone()[0]
                tx_ref = f"PAY-2026-{count_pmt + 1:04d}"
                conn.execute('''INSERT INTO payments (transaction_ref, booking_id, customer_id, amount, payment_method, transaction_status, notes)
                                VALUES (?, ?, ?, ?, ?, 'Success', 'Final checkout balance settlement')''',
                             (tx_ref, booking_id, booking['customer_id'], amount_to_pay, pay_method))
                conn.execute('UPDATE bookings SET payment_status = \'Paid\' WHERE id = ?', (booking_id,))
                conn.commit()

            # Refresh financials after checkout payment
            expenses = conn.execute('SELECT * FROM expenses WHERE booking_id = ?', (booking_id,)).fetchall()
            payments = conn.execute('SELECT * FROM payments WHERE booking_id = ?', (booking_id,)).fetchall()
            fin = calculate_booking_financials(dict(booking), [dict(e) for e in expenses], [dict(p) for p in payments])
            flash(f"Checkout payment of {format_inr(amount_to_pay)} confirmed via {pay_method}!", 'success')
            conn.close()
            return redirect(url_for('checkout_billing', booking_id=booking_id))

        # Scenario 2: Complete Checkout (Requires zero outstanding balance unless Admin override)
        elif action == 'complete_checkout':
            if fin['outstanding_balance'] > 0:
                flash(f"Cannot complete checkout. Outstanding balance of {format_inr(fin['outstanding_balance'])} remains unpaid.", 'error')
                conn.close()
                return redirect(url_for('checkout_billing', booking_id=booking_id))

            # Mark booking as Checked Out
            conn.execute('UPDATE bookings SET status = \'Checked Out\', payment_status = \'Paid\' WHERE id = ?', (booking_id,))
            
            # Generate invoice record
            count_inv = conn.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]
            inv_ref = f"INV-2026-{count_inv + 1:04d}"

            itemized_data = {
                'breakdown': fin['expense_breakdown_fmt'],
                'expenses': [dict(e) for e in fin['finalized_expenses']],
                'payments': [dict(p) for p in fin['successful_payments']]
            }

            inv_status = 'Settled' if fin['outstanding_balance'] <= 0 else 'Pending Settlement'
            conn.execute('''INSERT INTO invoices (invoice_ref, booking_id, customer_id, total_charges, total_payments, outstanding_balance, invoice_status, itemized_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                         (inv_ref, booking_id, booking['customer_id'], fin['total_charges'], fin['total_paid'], fin['outstanding_balance'], inv_status, json.dumps(itemized_data)))
            conn.commit()
            conn.close()

            flash(f"Checkout completed! Final invoice {inv_ref} generated.", 'success')
            return redirect(url_for('view_invoice', booking_id=booking_id))

    conn.close()
    return render_template('checkout.html', booking=booking, expenses=finalized_ledger_items, payments=payments, pending_orders=pending_orders, financials=fin)

@app.route('/bookings/<int:booking_id>/invoice')
def view_invoice(booking_id):
    """Printable, final itemized tax invoice."""
    conn = get_db_connection()
    booking = conn.execute('SELECT b.*, r.image_url, r.size_sqft, r.bed_type FROM bookings b LEFT JOIN rooms r ON b.room_id = r.id WHERE b.id = ?', (booking_id,)).fetchone()
    if not booking:
        conn.close()
        flash('Booking not found.', 'error')
        return redirect(url_for('my_bookings'))

    if not can_access_booking(booking):
        conn.close()
        flash('Access restricted. You are not authorized to view this invoice.', 'error')
        return redirect(url_for('my_bookings'))

    invoice = conn.execute('SELECT * FROM invoices WHERE booking_id = ?', (booking_id,)).fetchone()
    expenses = conn.execute('SELECT * FROM expenses WHERE booking_id = ? ORDER BY created_at ASC', (booking_id,)).fetchall()
    payments = conn.execute('SELECT * FROM payments WHERE booking_id = ? ORDER BY transaction_date ASC', (booking_id,)).fetchall()
    conn.close()

    fin = calculate_booking_financials(dict(booking), [dict(e) for e in expenses], [dict(p) for p in payments])
    finalized_ledger_items = fin['finalized_expenses']
    
    # If invoice doesn't exist yet (preview mode before checkout), generate preview reference
    inv_ref = invoice['invoice_ref'] if invoice else f"INV-PREVIEW-{booking_id:04d}"

    return render_template('invoice.html', booking=booking, invoice=invoice, inv_ref=inv_ref, expenses=finalized_ledger_items, payments=payments, financials=fin)

# ----------------- ADMIN DASHBOARD & EXPENSE MANAGEMENT ----------------- #

@app.route('/admin/expenses', methods=['GET', 'POST'])
@admin_required
def admin_expenses():

    conn = get_db_connection()

    if request.method == 'POST':
        # Add manual expense
        booking_id = int(request.form.get('booking_id'))
        category = request.form.get('category', 'Additional Services')
        description = request.form.get('description', '').strip()
        quantity = int(request.form.get('quantity', 1))
        unit_price = float(request.form.get('unit_price', 0.0))

        if not description or unit_price < 0 or quantity < 1:
            flash('Valid description, quantity and price are required.', 'error')
        else:
            booking = conn.execute('SELECT customer_id FROM bookings WHERE id = ?', (booking_id,)).fetchone()
            if booking:
                total_amt = round(quantity * unit_price, 2)
                count_exp = conn.execute('SELECT COUNT(*) FROM expenses').fetchone()[0]
                exp_ref = f"EXP-MN-{count_exp + 1:04d}"

                conn.execute('''INSERT INTO expenses (expense_ref, booking_id, customer_id, category, description, quantity, unit_price, total_amount, expense_status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Finalized')''',
                             (exp_ref, booking_id, booking['customer_id'], category, description, quantity, unit_price, total_amt))
                conn.commit()
                flash(f"Manual expense {exp_ref} of {format_inr(total_amt)} added to booking folio #{booking_id}!", 'success')
                conn.close()
                return redirect(url_for('admin_expenses', booking_id=booking_id))

    # Filters
    search_q = request.args.get('q', '').strip()
    selected_bid = request.args.get('booking_id', type=int)

    all_bookings = conn.execute('''SELECT b.id, b.booking_code, b.guest_name, b.customer_id, b.room_name, b.status, b.payment_status 
                                   FROM bookings b ORDER BY b.created_at DESC''').fetchall()

    if selected_bid:
        expenses = conn.execute('SELECT * FROM expenses WHERE booking_id = ? ORDER BY created_at DESC', (selected_bid,)).fetchall()
        payments = conn.execute('SELECT * FROM payments WHERE booking_id = ? ORDER BY transaction_date DESC', (selected_bid,)).fetchall()
        active_b = conn.execute('SELECT * FROM bookings WHERE id = ?', (selected_bid,)).fetchone()
        fin = calculate_booking_financials(dict(active_b), [dict(e) for e in expenses], [dict(p) for p in payments]) if active_b else None
    else:
        expenses = conn.execute('SELECT * FROM expenses ORDER BY created_at DESC LIMIT 30').fetchall()
        payments = conn.execute('SELECT * FROM payments ORDER BY transaction_date DESC LIMIT 20').fetchall()
        fin = None
        active_b = None

    conn.close()
    return render_template('admin/expenses.html', bookings=all_bookings, expenses=expenses, payments=payments, selected_bid=selected_bid, active_b=active_b, financials=fin, search_q=search_q)

@app.route('/admin/food-orders')
@admin_required
def admin_food_orders():

    conn = get_db_connection()
    orders = conn.execute('''SELECT fo.*, b.booking_code, b.room_name, b.guest_name 
                             FROM food_orders fo 
                             JOIN bookings b ON fo.booking_id = b.id 
                             ORDER BY fo.order_date DESC''').fetchall()
    conn.close()
    return render_template('admin/food_orders.html', orders=orders)

@app.route('/api/food-orders/<int:order_id>/track')
def api_track_food_order(order_id):
    """Retrieve detailed order tracking progress, status timeline, and itemized breakdown."""
    conn = get_db_connection()
    order = conn.execute('''SELECT fo.*, b.booking_code, b.room_name, b.guest_name 
                            FROM food_orders fo 
                            JOIN bookings b ON fo.booking_id = b.id 
                            WHERE fo.id = ?''', (order_id,)).fetchone()
    conn.close()

    if not order:
        return jsonify({'status': 'error', 'message': 'Order not found.'}), 404

    items = []
    try:
        items = json.loads(order['items_json']) if order['items_json'] else []
    except Exception:
        items = []

    timeline = {}
    try:
        timeline = json.loads(order['status_timeline_json']) if order['status_timeline_json'] else {}
    except Exception:
        timeline = {}

    # Define stage sequence
    stages = ['Placed', 'Preparing', 'Ready', 'Out for Delivery', 'Delivered']
    current_status = order['order_status']
    current_idx = stages.index(current_status) if current_status in stages else -1

    steps = []
    for idx, stage_name in enumerate(stages):
        is_completed = (current_idx >= 0 and idx <= current_idx)
        is_current = (stage_name == current_status)
        timestamp = timeline.get(stage_name, '')

        steps.append({
            'stage': stage_name,
            'completed': is_completed,
            'current': is_current,
            'timestamp': timestamp
        })

    is_delivered = (current_status == 'Delivered')
    is_cancelled = (current_status == 'Cancelled')

    return jsonify({
        'status': 'success',
        'order_id': order['id'],
        'order_ref': order['order_ref'],
        'order_status': current_status,
        'order_date': str(order['order_date']),
        'room_name': order['room_name'],
        'guest_name': order['guest_name'],
        'booking_code': order['booking_code'],
        'items': items,
        'item_count': sum(i.get('quantity', 1) for i in items),
        'total_amount': order['total_amount'],
        'total_amount_fmt': format_inr(order['total_amount']),
        'delivery_notes': order['delivery_notes'] or '',
        'steps': steps,
        'is_delivered': is_delivered,
        'is_cancelled': is_cancelled,
        'billing_status': 'Billed in Folio' if is_delivered else ('Excluded' if is_cancelled else 'Pending Delivery (Not billed yet)')
    })

@app.route('/admin/food-orders/<int:order_id>/update-status', methods=['POST'])
@admin_required
def update_food_order_status(order_id):
    """Admin updates status. If Delivered, creates finalized expense ledger entry."""
    new_status = request.form.get('order_status')
    conn = get_db_connection()
    order = conn.execute('SELECT * FROM food_orders WHERE id = ?', (order_id,)).fetchone()
    
    if not order:
        conn.close()
        flash('Order not found.', 'error')
        return redirect(url_for('admin_food_orders'))

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    timeline = {}
    try:
        timeline = json.loads(order['status_timeline_json']) if order['status_timeline_json'] else {}
    except Exception:
        timeline = {}

    timeline[new_status] = now_str

    conn.execute('UPDATE food_orders SET order_status = ?, status_timeline_json = ? WHERE id = ?', 
                 (new_status, json.dumps(timeline), order_id))

    # If transitioning to Delivered and no expense exists yet, add to expenses ledger under Food & Beverages
    if new_status == 'Delivered':
        existing_exp = conn.execute('SELECT id FROM expenses WHERE order_id = ?', (order_id,)).fetchone()
        if not existing_exp:
            count_exp = conn.execute('SELECT COUNT(*) FROM expenses').fetchone()[0]
            exp_ref = f"EXP-FO-{count_exp + 1:04d}"
            desc = f"Food & Room Service Order {order['order_ref']}"
            conn.execute('''INSERT INTO expenses (expense_ref, booking_id, customer_id, category, description, quantity, unit_price, total_amount, expense_status, order_id)
                            VALUES (?, ?, ?, 'Food & Beverages', ?, 1, ?, ?, 'Finalized', ?)''',
                         (exp_ref, order['booking_id'], order['customer_id'], desc, order['total_amount'], order['total_amount'], order_id))
            
            # Recalculate booking payment_status with new finalized expense
            b_row = conn.execute('SELECT * FROM bookings WHERE id = ?', (order['booking_id'],)).fetchone()
            if b_row:
                b_exps = conn.execute('SELECT * FROM expenses WHERE booking_id = ?', (order['booking_id'],)).fetchall()
                b_pmts = conn.execute('SELECT * FROM payments WHERE booking_id = ?', (order['booking_id'],)).fetchall()
                fin = calculate_booking_financials(dict(b_row), [dict(e) for e in b_exps], [dict(p) for p in b_pmts])
                conn.execute('UPDATE bookings SET payment_status = ? WHERE id = ?', (fin['payment_status'], order['booking_id']))

            flash(f"Order {order['order_ref']} marked as Delivered and added to guest ledger ({format_inr(order['total_amount'])}).", 'success')
    elif new_status == 'Cancelled':
        # Remove from expenses ledger if was previously added
        conn.execute('DELETE FROM expenses WHERE order_id = ?', (order_id,))

        # Recalculate booking payment_status after removing expense
        b_row = conn.execute('SELECT * FROM bookings WHERE id = ?', (order['booking_id'],)).fetchone()
        if b_row:
            b_exps = conn.execute('SELECT * FROM expenses WHERE booking_id = ?', (order['booking_id'],)).fetchall()
            b_pmts = conn.execute('SELECT * FROM payments WHERE booking_id = ?', (order['booking_id'],)).fetchall()
            fin = calculate_booking_financials(dict(b_row), [dict(e) for e in b_exps], [dict(p) for p in b_pmts])
            conn.execute('UPDATE bookings SET payment_status = ? WHERE id = ?', (fin['payment_status'], order['booking_id']))

        flash(f"Order {order['order_ref']} marked as Cancelled. Any related ledger charges removed.", 'info')

    conn.commit()
    conn.close()
    return redirect(url_for('admin_food_orders'))

@app.route('/experiences')
def experiences():
    return render_template('experiences.html')

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        subject = request.form.get('subject')
        message = request.form.get('message')
        
        conn = get_db_connection()
        conn.execute('''INSERT INTO inquiries (name, email, phone, subject, message)
                        VALUES (?, ?, ?, ?, ?)''', (name, email, phone, subject, message))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'Thank you! Your message has been sent to our guest service desk.'})
    return render_template('contact.html')

@app.route('/api/book', methods=['POST'])
def api_book():
    return api_confirm_booking()

@app.route('/api/newsletter', methods=['POST'])
def api_newsletter():
    data = request.get_json() or request.form
    email = data.get('email')
    if not email:
        return jsonify({'status': 'error', 'message': 'Email address required.'}), 400
    
    conn = get_db_connection()
    try:
        conn.execute('INSERT OR IGNORE INTO newsletter (email) VALUES (?)', (email,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success', 'message': 'Thank you for subscribing to RETREAT offers and updates.'})

# ----------------- ADMIN DASHBOARD ROUTES ----------------- #

@app.route('/admin')
@admin_required
def admin_dashboard():

    conn = get_db_connection()
    bookings = [dict(r) for r in conn.execute('SELECT * FROM bookings ORDER BY created_at DESC').fetchall()]
    expenses = [dict(r) for r in conn.execute('SELECT * FROM expenses').fetchall()]
    payments = [dict(r) for r in conn.execute('SELECT * FROM payments').fetchall()]
    prooms = [dict(r) for r in conn.execute('SELECT pr.*, r.name as category_name, r.category as category_type, r.price_per_night, b.guest_name as current_guest_name, b.booking_code as current_booking_code FROM physical_rooms pr JOIN rooms r ON pr.room_category_id = r.id LEFT JOIN bookings b ON pr.current_booking_id = b.id ORDER BY pr.floor ASC, pr.room_number ASC').fetchall()]
    food_orders = [dict(r) for r in conn.execute('SELECT * FROM food_orders').fetchall()]
    service_reqs = [dict(r) for r in conn.execute('SELECT * FROM service_requests').fetchall()]
    rooms = [dict(r) for r in conn.execute('SELECT * FROM rooms ORDER BY price_per_night ASC').fetchall()]
    total_inquiries = conn.execute('SELECT COUNT(*) FROM inquiries').fetchone()[0]

    # Pre-calculate financial balances for recent bookings to display in dashboard table
    exp_by_bid = {}
    for e in expenses:
        exp_by_bid.setdefault(e['booking_id'], []).append(e)
    pmt_by_bid = {}
    for p in payments:
        pmt_by_bid.setdefault(p['booking_id'], []).append(p)

    recent_bookings_with_fin = []
    for b in bookings[:8]:
        bid = b['id']
        b_fin = calculate_booking_financials(b, exp_by_bid.get(bid, []), pmt_by_bid.get(bid, []))
        b_copy = dict(b)
        b_copy['financials'] = b_fin
        recent_bookings_with_fin.append(b_copy)

    recent_orders = conn.execute('''SELECT fo.*, b.booking_code, b.room_name, b.guest_name, b.assigned_room_number 
                                    FROM food_orders fo 
                                    JOIN bookings b ON fo.booking_id = b.id 
                                    ORDER BY fo.order_date DESC LIMIT 5''').fetchall()

    recent_requests = conn.execute('''SELECT sr.*, b.booking_code, b.guest_name 
                                      FROM service_requests sr 
                                      JOIN bookings b ON sr.booking_id = b.id 
                                      ORDER BY sr.created_at DESC LIMIT 5''').fetchall()
    conn.close()

    metrics = calculate_kpi_metrics(
        all_rooms_count=len(prooms),
        all_bookings=bookings,
        all_expenses=expenses,
        all_payments=payments,
        physical_rooms=prooms,
        food_orders=food_orders,
        service_requests=service_reqs,
        period_days=30
    )
    metrics['total_inquiries'] = total_inquiries

    return render_template(
        'admin/dashboard.html',
        stats=metrics,
        bookings=recent_bookings_with_fin,
        rooms=rooms,
        physical_rooms=prooms,
        recent_orders=recent_orders,
        recent_requests=recent_requests
    )

@app.route('/admin/bookings')
@admin_required
def admin_bookings():

    conn = get_db_connection()
    status_filter = request.args.get('status', '').strip()
    search_q = request.args.get('q', '').strip()
    date_from = request.args.get('from', '').strip()
    date_to = request.args.get('to', '').strip()

    query = 'SELECT * FROM bookings WHERE 1=1'
    params = []

    if status_filter:
        query += ' AND status = ?'
        params.append(status_filter)

    if search_q:
        query += ' AND (LOWER(guest_name) LIKE ? OR LOWER(booking_code) LIKE ? OR LOWER(guest_email) LIKE ? OR assigned_room_number LIKE ?)'
        wildcard = f"%{search_q.lower()}%"
        params.extend([wildcard, wildcard, wildcard, f"%{search_q}%"])

    if date_from:
        query += ' AND check_in >= ?'
        params.append(date_from)
    if date_to:
        query += ' AND check_out <= ?'
        params.append(date_to)

    query += ' ORDER BY created_at DESC'
    raw_bookings = conn.execute(query, params).fetchall()

    expenses = [dict(r) for r in conn.execute('SELECT * FROM expenses').fetchall()]
    payments = [dict(r) for r in conn.execute('SELECT * FROM payments').fetchall()]
    all_physical_rooms = conn.execute('SELECT * FROM physical_rooms ORDER BY room_number ASC').fetchall()
    conn.close()

    exp_by_bid = {}
    for e in expenses:
        exp_by_bid.setdefault(e['booking_id'], []).append(e)
    pmt_by_bid = {}
    for p in payments:
        pmt_by_bid.setdefault(p['booking_id'], []).append(p)

    bookings = []
    for b in raw_bookings:
        b_dict = dict(b)
        bid = b_dict['id']
        b_dict['financials'] = calculate_booking_financials(b_dict, exp_by_bid.get(bid, []), pmt_by_bid.get(bid, []))
        bookings.append(b_dict)

    return render_template(
        'admin/bookings.html',
        bookings=bookings,
        current_status=status_filter,
        search_q=search_q,
        date_from=date_from,
        date_to=date_to,
        physical_rooms=all_physical_rooms
    )

@app.route('/admin/bookings/<int:booking_id>/assign-room', methods=['POST'])
@admin_required
def admin_assign_room(booking_id):
    """Assign or reassign a specific physical room to a reservation."""
    room_number = request.form.get('room_number', '').strip()
    conn = get_db_connection()
    booking = conn.execute('SELECT * FROM bookings WHERE id = ?', (booking_id,)).fetchone()

    if not booking:
        conn.close()
        flash('Booking not found.', 'error')
        return redirect(url_for('admin_bookings'))

    if booking['status'] in ['Cancelled', 'Completed', 'Checked Out']:
        conn.close()
        flash(f"Cannot assign room to a {booking['status']} reservation.", 'error')
        return redirect(url_for('admin_bookings'))

    if not room_number:
        # Clear assignment
        old_room_id = booking['assigned_room_id']
        if old_room_id:
            conn.execute("UPDATE physical_rooms SET operational_status = 'Available', current_booking_id = NULL WHERE id = ?", (old_room_id,))
        conn.execute('UPDATE bookings SET assigned_room_id = NULL, assigned_room_number = NULL WHERE id = ?', (booking_id,))
        conn.commit()
        conn.close()
        flash(f"Room unassigned from booking {booking['booking_code']}.", 'info')
        return redirect(url_for('admin_bookings'))

    # Check physical room eligibility
    proom = conn.execute('SELECT * FROM physical_rooms WHERE room_number = ?', (room_number,)).fetchone()
    if not proom:
        conn.close()
        flash(f"Room {room_number} does not exist in inventory.", 'error')
        return redirect(url_for('admin_bookings'))

    # Category validation
    if proom['room_category_id'] != booking['room_id']:
        conn.close()
        flash(f"Room {room_number} does not match the booked category ({booking['room_name']}).", 'error')
        return redirect(url_for('admin_bookings'))

    # Date overlap & restriction check
    active_assignments = [dict(r) for r in conn.execute('SELECT * FROM bookings WHERE assigned_room_number IS NOT NULL AND status NOT IN (\'Cancelled\', \'Completed\', \'Checked Out\')').fetchall()]
    avail_check = is_physical_room_available(
        room_number=room_number,
        check_in_str=booking['check_in'],
        check_out_str=booking['check_out'],
        active_assignments=active_assignments,
        current_operational_status=proom['operational_status'],
        exclude_booking_id=booking_id
    )

    if not avail_check['available']:
        conn.close()
        flash(f"Cannot assign Room {room_number}: {avail_check['reason']}", 'error')
        return redirect(url_for('admin_bookings'))

    # Free previous room if changed
    if booking['assigned_room_id'] and booking['assigned_room_id'] != proom['id']:
        conn.execute("UPDATE physical_rooms SET operational_status = 'Available', current_booking_id = NULL WHERE id = ?", (booking['assigned_room_id'],))

    # Update booking
    conn.execute('UPDATE bookings SET assigned_room_id = ?, assigned_room_number = ? WHERE id = ?', (proom['id'], room_number, booking_id))
    
    # If booking is already Checked In, set room status to Occupied
    if booking['status'] == 'Checked In':
        conn.execute("UPDATE physical_rooms SET operational_status = 'Occupied', current_booking_id = ? WHERE id = ?", (booking_id, proom['id']))

    conn.commit()
    conn.close()
    flash(f"Room {room_number} successfully assigned to booking {booking['booking_code']} ({booking['guest_name']}).", 'success')
    return redirect(url_for('admin_bookings'))

@app.route('/admin/checkin-checkout')
@admin_required
def admin_checkin_checkout():
    """Consolidated Front Desk operational control for today's and active guest movements."""
    conn = get_db_connection()
    today_str = date.today().strftime('%Y-%m-%d')
    
    # Bookings eligible for Check-In: Confirmed
    checkin_queue = conn.execute('''SELECT b.*, r.category as room_category 
                                    FROM bookings b 
                                    JOIN rooms r ON b.room_id = r.id 
                                    WHERE b.status = 'Confirmed' 
                                    ORDER BY b.check_in ASC''').fetchall()

    # Bookings currently in-house eligible for Check-Out: Checked In
    checkout_queue_raw = conn.execute('''SELECT b.*, r.category as room_category 
                                         FROM bookings b 
                                         JOIN rooms r ON b.room_id = r.id 
                                         WHERE b.status = 'Checked In' 
                                         ORDER BY b.check_out ASC''').fetchall()

    expenses = [dict(r) for r in conn.execute('SELECT * FROM expenses').fetchall()]
    payments = [dict(r) for r in conn.execute('SELECT * FROM payments').fetchall()]
    physical_rooms = conn.execute('SELECT * FROM physical_rooms ORDER BY room_number ASC').fetchall()
    conn.close()

    exp_by_bid = {}
    for e in expenses:
        exp_by_bid.setdefault(e['booking_id'], []).append(e)
    pmt_by_bid = {}
    for p in payments:
        pmt_by_bid.setdefault(p['booking_id'], []).append(p)

    checkout_queue = []
    for b in checkout_queue_raw:
        b_dict = dict(b)
        bid = b_dict['id']
        b_dict['financials'] = calculate_booking_financials(b_dict, exp_by_bid.get(bid, []), pmt_by_bid.get(bid, []))
        checkout_queue.append(b_dict)

    return render_template(
        'admin/checkin_checkout.html',
        checkin_queue=checkin_queue,
        checkout_queue=checkout_queue,
        today_str=today_str,
        physical_rooms=physical_rooms
    )

@app.route('/admin/bookings/<int:booking_id>/check-in', methods=['GET', 'POST'])
@admin_required
def admin_checkin_flow(booking_id):
    """Front Desk guest check-in interface and validation."""
    conn = get_db_connection()
    booking = conn.execute('''SELECT b.*, r.name as category_name, r.category as category_type, r.capacity_adults, r.max_occupancy, r.price_per_night 
                             FROM bookings b 
                             JOIN rooms r ON b.room_id = r.id 
                             WHERE b.id = ?''', (booking_id,)).fetchone()

    if not booking:
        conn.close()
        flash('Booking not found.', 'error')
        return redirect(url_for('admin_checkin_checkout'))

    if booking['status'] == 'Checked In':
        conn.close()
        flash(f"Booking {booking['booking_code']} is already Checked In.", 'info')
        return redirect(url_for('admin_checkin_checkout'))

    if booking['status'] in ['Cancelled', 'Completed', 'Checked Out']:
        conn.close()
        flash(f"Cannot check in a {booking['status']} reservation.", 'error')
        return redirect(url_for('admin_checkin_checkout'))

    # Available physical rooms of this category
    active_assignments = [dict(r) for r in conn.execute('SELECT * FROM bookings WHERE assigned_room_number IS NOT NULL AND status NOT IN (\'Cancelled\', \'Completed\', \'Checked Out\')').fetchall()]
    all_cat_rooms = conn.execute('SELECT * FROM physical_rooms WHERE room_category_id = ? ORDER BY room_number ASC', (booking['room_id'],)).fetchall()

    eligible_rooms = []
    for pr in all_cat_rooms:
        avail = is_physical_room_available(
            room_number=pr['room_number'],
            check_in_str=booking['check_in'],
            check_out_str=booking['check_out'],
            active_assignments=active_assignments,
            current_operational_status=pr['operational_status'],
            exclude_booking_id=booking_id
        )
        eligible_rooms.append({
            'room': dict(pr),
            'available': avail['available'],
            'reason': avail['reason']
        })

    if request.method == 'POST':
        selected_room_number = request.form.get('assigned_room_number', '').strip()
        if not selected_room_number:
            conn.close()
            flash('Please select an available physical room number for check-in.', 'error')
            return redirect(url_for('admin_checkin_flow', booking_id=booking_id))

        target_room = conn.execute('SELECT * FROM physical_rooms WHERE room_number = ?', (selected_room_number,)).fetchone()
        if not target_room or target_room['room_category_id'] != booking['room_id']:
            conn.close()
            flash('Selected room is invalid or category mismatched.', 'error')
            return redirect(url_for('admin_checkin_flow', booking_id=booking_id))

        if target_room['operational_status'] in ['Maintenance', 'Out of Service']:
            conn.close()
            flash(f"Room {selected_room_number} is currently under {target_room['operational_status']}.", 'error')
            return redirect(url_for('admin_checkin_flow', booking_id=booking_id))

        # Check if already occupied
        if target_room['operational_status'] == 'Occupied' and target_room['current_booking_id'] != booking_id:
            conn.close()
            flash(f"Room {selected_room_number} is already occupied by another in-house guest.", 'error')
            return redirect(url_for('admin_checkin_flow', booking_id=booking_id))

        # Perform Check-In
        now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''UPDATE bookings 
                        SET status = 'Checked In', 
                            assigned_room_id = ?, 
                            assigned_room_number = ?, 
                            actual_check_in = ? 
                        WHERE id = ?''',
                     (target_room['id'], selected_room_number, now_ts, booking_id))

        conn.execute('''UPDATE physical_rooms 
                        SET operational_status = 'Occupied', 
                            current_booking_id = ? 
                        WHERE id = ?''',
                     (booking_id, target_room['id']))

        conn.commit()
        conn.close()
        flash(f"Guest {booking['guest_name']} successfully Checked In to Room {selected_room_number}! (Ref: {booking['booking_code']})", 'success')
        return redirect(url_for('admin_checkin_checkout'))

    conn.close()
    return render_template(
        'admin/checkin_modal.html',
        booking=booking,
        eligible_rooms=eligible_rooms
    )

@app.route('/admin/bookings/<int:booking_id>/checkout', methods=['GET', 'POST'])
@admin_required
def admin_checkout_flow(booking_id):
    """Front Desk guest checkout, balance settlement, and room release."""
    conn = get_db_connection()
    booking = conn.execute('''SELECT b.*, r.name as category_name, r.category as category_type, r.price_per_night 
                             FROM bookings b 
                             JOIN rooms r ON b.room_id = r.id 
                             WHERE b.id = ?''', (booking_id,)).fetchone()

    if not booking:
        conn.close()
        flash('Booking not found.', 'error')
        return redirect(url_for('admin_checkin_checkout'))

    if booking['status'] == 'Checked Out':
        conn.close()
        flash(f"Booking {booking['booking_code']} has already completed checkout.", 'info')
        return redirect(url_for('view_invoice', booking_id=booking_id))

    expenses = [dict(r) for r in conn.execute('SELECT * FROM expenses WHERE booking_id = ? ORDER BY created_at ASC', (booking_id,)).fetchall()]
    payments = [dict(r) for r in conn.execute('SELECT * FROM payments WHERE booking_id = ? ORDER BY transaction_date ASC', (booking_id,)).fetchall()]
    pending_orders = conn.execute('SELECT * FROM food_orders WHERE booking_id = ? AND order_status NOT IN (\'Delivered\', \'Cancelled\')', (booking_id,)).fetchall()

    fin = calculate_booking_financials(dict(booking), expenses, payments)

    if request.method == 'POST':
        action = request.form.get('action')

        # Action 1: Record Front Desk Settlement Payment
        if action == 'record_payment':
            pay_amt = float(request.form.get('payment_amount', 0.0))
            pay_method = request.form.get('payment_method', 'Credit/Debit Card')
            pay_notes = request.form.get('payment_notes', 'Front Desk Settlement at Checkout').strip()

            if pay_amt <= 0:
                conn.close()
                flash('Please enter a valid payment amount greater than zero.', 'error')
                return redirect(url_for('admin_checkout_flow', booking_id=booking_id))

            count_pmt = conn.execute('SELECT COUNT(*) FROM payments').fetchone()[0]
            tx_ref = f"PAY-2026-{count_pmt + 1:04d}"

            conn.execute('''INSERT INTO payments (transaction_ref, booking_id, customer_id, amount, payment_method, transaction_status, notes)
                            VALUES (?, ?, ?, ?, ?, 'Success', ?)''',
                         (tx_ref, booking_id, booking['customer_id'], pay_amt, pay_method, pay_notes))

            # Re-evaluate payment status
            new_pmts = [dict(r) for r in conn.execute('SELECT * FROM payments WHERE booking_id = ?', (booking_id,)).fetchall()]
            new_fin = calculate_booking_financials(dict(booking), expenses, new_pmts)
            conn.execute('UPDATE bookings SET payment_status = ? WHERE id = ?', (new_fin['payment_status'], booking_id))
            conn.commit()
            conn.close()

            flash(f"Payment of {format_inr(pay_amt)} ({pay_method}) recorded successfully! Ref: {tx_ref}", 'success')
            return redirect(url_for('admin_checkout_flow', booking_id=booking_id))

        # Action 2: Finalize Checkout
        elif action == 'complete_checkout':
            override_reason = request.form.get('override_reason', '').strip()

            if fin['outstanding_balance'] > 0 and not override_reason:
                conn.close()
                flash(f"Cannot complete checkout. Outstanding balance of {format_inr(fin['outstanding_balance'])} remains unpaid. Record payment or provide an authorized exception reason.", 'error')
                return redirect(url_for('admin_checkout_flow', booking_id=booking_id))

            now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # Release physical room to 'Cleaning' status
            if booking['assigned_room_id']:
                conn.execute('''UPDATE physical_rooms 
                                SET operational_status = 'Cleaning', 
                                    current_booking_id = NULL, 
                                    notes = ? 
                                WHERE id = ?''',
                             (f"Turnover cleaning post checkout ({booking['booking_code']})", booking['assigned_room_id']))

            # Update booking status
            final_payment_status = 'Paid' if fin['outstanding_balance'] <= 0 else 'Settled (Exception)'
            conn.execute('''UPDATE bookings 
                            SET status = 'Checked Out', 
                                payment_status = ?, 
                                actual_check_out = ? 
                            WHERE id = ?''',
                         (final_payment_status, now_ts, booking_id))

            # Generate Invoice
            count_inv = conn.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]
            inv_ref = f"INV-2026-{count_inv + 1:04d}"
            itemized_data = {
                'breakdown': fin['expense_breakdown_fmt'],
                'expenses': [dict(e) for e in fin['finalized_expenses']],
                'payments': [dict(p) for p in fin['successful_payments']],
                'override_reason': override_reason
            }

            inv_status = 'Settled' if fin['outstanding_balance'] <= 0 else 'Pending Settlement'
            conn.execute('''INSERT INTO invoices (invoice_ref, booking_id, customer_id, total_charges, total_payments, outstanding_balance, invoice_status, itemized_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                         (inv_ref, booking_id, booking['customer_id'], fin['total_charges'], fin['total_paid'], fin['outstanding_balance'], inv_status, json.dumps(itemized_data)))

            conn.commit()
            conn.close()
            flash(f"Checkout successfully completed for {booking['guest_name']}! Room {booking['assigned_room_number'] or ''} scheduled for cleaning. Invoice {inv_ref} generated.", 'success')
            return redirect(url_for('admin_checkin_checkout'))

    conn.close()
    return render_template(
        'admin/checkout_modal.html',
        booking=booking,
        financials=fin,
        expenses=fin['finalized_expenses'],
        payments=payments,
        pending_orders=pending_orders
    )

@app.route('/admin/rooms', methods=['GET', 'POST'])
@admin_required
def admin_rooms():

    conn = get_db_connection()
    if request.method == 'POST':
        action = request.form.get('action')

        # Add room category
        if action == 'add_category':
            name = request.form.get('name')
            category = request.form.get('category')
            tagline = request.form.get('tagline')
            description = request.form.get('description')
            price = float(request.form.get('price_per_night', 2499))
            adults = int(request.form.get('capacity_adults', 2))
            children = int(request.form.get('capacity_children', 0))
            max_occ = int(request.form.get('max_occupancy', 2))
            extra_charge = float(request.form.get('additional_guest_charge', 0.0))
            inventory = int(request.form.get('total_inventory', 5))
            size = int(request.form.get('size_sqft', 300))
            bed_type = request.form.get('bed_type', 'King Bed')
            view = request.form.get('view', 'City / Garden View')
            image_url = request.form.get('image_url')
            badge = request.form.get('badge', '')
            features = request.form.get('features', '')

            conn.execute('''INSERT INTO rooms 
                (name, category, tagline, description, price_per_night, capacity_adults, capacity_children, 
                 max_occupancy, additional_guest_charge, total_inventory, size_sqft, bed_type, view, badge, image_url, features, is_available)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)''',
                (name, category, tagline, description, price, adults, children, max_occ, extra_charge, inventory, size, bed_type, view, badge, image_url, features))
            conn.commit()
            flash(f"Category '{name}' created.", 'success')
            conn.close()
            return redirect(url_for('admin_rooms'))

        # Update physical room operational status
        elif action == 'update_room_status':
            room_id = int(request.form.get('physical_room_id'))
            new_status = request.form.get('operational_status')
            notes = request.form.get('notes', '').strip()

            target_pr = conn.execute('SELECT * FROM physical_rooms WHERE id = ?', (room_id,)).fetchone()
            if target_pr:
                # If currently occupied by an active checked-in booking, prevent manual status switch unless checkout
                if target_pr['operational_status'] == 'Occupied' and new_status != 'Occupied':
                    conn.close()
                    flash(f"Room {target_pr['room_number']} is currently occupied by an in-house guest. Please complete checkout first.", 'error')
                    return redirect(url_for('admin_rooms'))

                conn.execute('UPDATE physical_rooms SET operational_status = ?, notes = ? WHERE id = ?', (new_status, notes or target_pr['notes'], room_id))
                conn.commit()
                flash(f"Room {target_pr['room_number']} status updated to '{new_status}'.", 'success')
            conn.close()
            return redirect(url_for('admin_rooms'))

    # Filters
    cat_filter = request.args.get('category_id', type=int)
    status_filter = request.args.get('status')
    search_q = request.args.get('q', '').strip()

    q_rooms = '''SELECT pr.*, r.name as category_name, r.category as category_type, r.price_per_night, 
                        r.capacity_adults, r.max_occupancy, b.booking_code, b.guest_name, b.check_in, b.check_out 
                 FROM physical_rooms pr 
                 JOIN rooms r ON pr.room_category_id = r.id 
                 LEFT JOIN bookings b ON pr.current_booking_id = b.id 
                 WHERE 1=1'''
    params = []

    if cat_filter:
        q_rooms += ' AND pr.room_category_id = ?'
        params.append(cat_filter)
    if status_filter:
        q_rooms += ' AND pr.operational_status = ?'
        params.append(status_filter)
    if search_q:
        q_rooms += ' AND (pr.room_number LIKE ? OR LOWER(r.name) LIKE ?)'
        params.extend([f"%{search_q}%", f"%{search_q.lower()}%"])

    q_rooms += ' ORDER BY pr.floor ASC, pr.room_number ASC'
    physical_rooms = conn.execute(q_rooms, params).fetchall()
    categories = conn.execute('SELECT * FROM rooms ORDER BY price_per_night ASC').fetchall()
    conn.close()

    return render_template(
        'admin/rooms.html',
        physical_rooms=physical_rooms,
        categories=categories,
        current_cat=cat_filter,
        current_status=status_filter,
        search_q=search_q
    )

@app.route('/admin/service-requests', methods=['GET', 'POST'])
@admin_required
def admin_service_requests():

    conn = get_db_connection()

    if request.method == 'POST':
        action = request.form.get('action')

        # Action: Create new request (Front desk dispatch on guest call)
        if action == 'create_request':
            booking_id = int(request.form.get('booking_id'))
            req_type = request.form.get('request_type', 'Housekeeping')
            desc = request.form.get('description', '').strip()
            priority = request.form.get('priority', 'Standard')
            is_chargeable = 1 if request.form.get('is_chargeable') == '1' else 0
            charge_amt = float(request.form.get('charge_amount', 0.0)) if is_chargeable else 0.0

            booking = conn.execute('SELECT customer_id, assigned_room_number FROM bookings WHERE id = ?', (booking_id,)).fetchone()
            if booking and desc:
                count_req = conn.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0]
                req_ref = f"REQ-2026-{count_req + 1:04d}"
                conn.execute('''INSERT INTO service_requests (request_ref, booking_id, customer_id, room_number, request_type, description, priority, is_chargeable, charge_amount)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                             (req_ref, booking_id, booking['customer_id'], booking['assigned_room_number'], req_type, desc, priority, is_chargeable, charge_amt))
                conn.commit()
                flash(f"Service request {req_ref} created and dispatched.", 'success')

        # Action: Update Status
        elif action == 'update_status':
            req_id = int(request.form.get('request_id'))
            new_status = request.form.get('status')
            notes = request.form.get('notes', '').strip()

            req_row = conn.execute('SELECT * FROM service_requests WHERE id = ?', (req_id,)).fetchone()
            if req_row:
                now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if new_status == 'Completed' else None

                # If completing a chargeable service, post to expense ledger exactly once
                if new_status == 'Completed' and req_row['is_chargeable'] and req_row['charge_amount'] > 0:
                    if not req_row['expense_id']:
                        count_exp = conn.execute('SELECT COUNT(*) FROM expenses').fetchone()[0]
                        exp_ref = f"EXP-SR-{count_exp + 1:04d}"
                        cat = 'Laundry' if req_row['request_type'] == 'Laundry' else 'Additional Services'
                        desc = f"{req_row['request_type']}: {req_row['description']} ({req_row['request_ref']})"

                        c_cur = conn.cursor()
                        c_cur.execute('''INSERT INTO expenses (expense_ref, booking_id, customer_id, category, description, quantity, unit_price, total_amount, expense_status)
                                         VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'Finalized')''',
                                      (exp_ref, req_row['booking_id'], req_row['customer_id'], cat, desc, req_row['charge_amount'], req_row['charge_amount']))
                        new_exp_id = c_cur.lastrowid
                        conn.execute('UPDATE service_requests SET status = ?, completed_at = ?, notes = ?, expense_id = ? WHERE id = ?',
                                     (new_status, now_ts, notes or req_row['notes'], new_exp_id, req_id))
                        
                        # Recalculate booking payment_status with new finalized expense
                        b_row = conn.execute('SELECT * FROM bookings WHERE id = ?', (req_row['booking_id'],)).fetchone()
                        if b_row:
                            b_exps = conn.execute('SELECT * FROM expenses WHERE booking_id = ?', (req_row['booking_id'],)).fetchall()
                            b_pmts = conn.execute('SELECT * FROM payments WHERE booking_id = ?', (req_row['booking_id'],)).fetchall()
                            fin = calculate_booking_financials(dict(b_row), [dict(e) for e in b_exps], [dict(p) for p in b_pmts])
                            conn.execute('UPDATE bookings SET payment_status = ? WHERE id = ?', (fin['payment_status'], req_row['booking_id']))

                        flash(f"Service {req_row['request_ref']} completed. Charge of {format_inr(req_row['charge_amount'])} billed to folio ({exp_ref}).", 'success')
                    else:
                        conn.execute('UPDATE service_requests SET status = ?, completed_at = ?, notes = ? WHERE id = ?',
                                     (new_status, now_ts, notes or req_row['notes'], req_id))
                        flash(f"Service request {req_row['request_ref']} marked as Completed.", 'info')

                elif new_status == 'Cancelled':
                    # If cancelled, remove any ledger expense if was posted
                    if req_row['expense_id']:
                        conn.execute('DELETE FROM expenses WHERE id = ?', (req_row['expense_id'],))
                        conn.execute('UPDATE service_requests SET status = ?, expense_id = NULL, notes = ? WHERE id = ?',
                                     (new_status, notes or req_row['notes'], req_id))

                        # Recalculate booking payment_status after removing expense
                        b_row = conn.execute('SELECT * FROM bookings WHERE id = ?', (req_row['booking_id'],)).fetchone()
                        if b_row:
                            b_exps = conn.execute('SELECT * FROM expenses WHERE booking_id = ?', (req_row['booking_id'],)).fetchall()
                            b_pmts = conn.execute('SELECT * FROM payments WHERE booking_id = ?', (req_row['booking_id'],)).fetchall()
                            fin = calculate_booking_financials(dict(b_row), [dict(e) for e in b_exps], [dict(p) for p in b_pmts])
                            conn.execute('UPDATE bookings SET payment_status = ? WHERE id = ?', (fin['payment_status'], req_row['booking_id']))

                        flash(f"Service request {req_row['request_ref']} cancelled and billable charge removed.", 'info')
                    else:
                        conn.execute('UPDATE service_requests SET status = ?, notes = ? WHERE id = ?',
                                     (new_status, notes or req_row['notes'], req_id))
                        flash(f"Service request {req_row['request_ref']} marked as Cancelled.", 'info')
                else:
                    conn.execute('UPDATE service_requests SET status = ?, notes = ? WHERE id = ?',
                                 (new_status, notes or req_row['notes'], req_id))
                    flash(f"Service request {req_row['request_ref']} status updated to '{new_status}'.", 'success')

                conn.commit()

        conn.close()
        return redirect(url_for('admin_service_requests'))

    # GET
    status_filter = request.args.get('status')
    query = '''SELECT sr.*, b.booking_code, b.guest_name, b.room_name 
               FROM service_requests sr 
               JOIN bookings b ON sr.booking_id = b.id 
               WHERE 1=1'''
    params = []
    if status_filter:
        query += ' AND sr.status = ?'
        params.append(status_filter)
    query += ' ORDER BY sr.created_at DESC'

    requests_list = conn.execute(query, params).fetchall()
    active_bookings = conn.execute('''SELECT id, booking_code, guest_name, assigned_room_number, room_name 
                                      FROM bookings 
                                      WHERE status IN ('Checked In', 'Confirmed') 
                                      ORDER BY guest_name ASC''').fetchall()
    conn.close()

    return render_template('admin/service_requests.html', requests=requests_list, active_bookings=active_bookings, current_status=status_filter)

@app.route('/admin/customers')
@admin_required
def admin_customers():

    conn = get_db_connection()
    search_q = request.args.get('q', '').strip()

    cust_query = 'SELECT * FROM customers WHERE 1=1'
    params = []
    if search_q:
        cust_query += ' AND (LOWER(full_name) LIKE ? OR LOWER(email) LIKE ? OR phone LIKE ? OR customer_id LIKE ?)'
        wildcard = f"%{search_q.lower()}%"
        params.extend([wildcard, wildcard, f"%{search_q}%", f"%{search_q}%"])

    cust_query += ' ORDER BY created_at DESC'
    raw_customers = conn.execute(cust_query, params).fetchall()

    bookings = [dict(r) for r in conn.execute('SELECT * FROM bookings').fetchall()]
    expenses = [dict(r) for r in conn.execute('SELECT * FROM expenses').fetchall()]
    payments = [dict(r) for r in conn.execute('SELECT * FROM payments').fetchall()]
    conn.close()

    # Index by customer_id
    bookings_by_cid = {}
    for b in bookings:
        cid = b.get('customer_id')
        if cid:
            bookings_by_cid.setdefault(cid, []).append(b)

    exp_by_bid = {}
    for e in expenses:
        exp_by_bid.setdefault(e['booking_id'], []).append(e)

    pmt_by_bid = {}
    for p in payments:
        pmt_by_bid.setdefault(p['booking_id'], []).append(p)

    customers = []
    for c in raw_customers:
        c_dict = dict(c)
        cid = c_dict['customer_id']
        c_bookings = bookings_by_cid.get(cid, [])

        total_bookings = len(c_bookings)
        active_bookings = sum(1 for b in c_bookings if b['status'] in ['Confirmed', 'Checked In'])
        
        lifetime_spend = 0.0
        outstanding_balance = 0.0

        for b in c_bookings:
            bid = b['id']
            b_fin = calculate_booking_financials(b, exp_by_bid.get(bid, []), pmt_by_bid.get(bid, []))
            lifetime_spend += b_fin['total_charges']
            if b_fin['outstanding_balance'] > 0:
                outstanding_balance += b_fin['outstanding_balance']

        c_dict['total_bookings'] = total_bookings
        c_dict['active_bookings'] = active_bookings
        c_dict['lifetime_spend'] = lifetime_spend
        c_dict['lifetime_spend_fmt'] = format_inr(lifetime_spend)
        c_dict['outstanding_balance'] = outstanding_balance
        c_dict['outstanding_balance_fmt'] = format_inr(outstanding_balance)
        customers.append(c_dict)

    return render_template('admin/customers.html', customers=customers, search_q=search_q)

@app.route('/admin/customers/<customer_id>')
@admin_required
def admin_customer_detail(customer_id):

    conn = get_db_connection()
    customer = conn.execute('SELECT * FROM customers WHERE customer_id = ?', (customer_id,)).fetchone()
    if not customer:
        conn.close()
        flash('Customer record not found.', 'error')
        return redirect(url_for('admin_customers'))

    bookings_raw = conn.execute('SELECT * FROM bookings WHERE customer_id = ? ORDER BY created_at DESC', (customer_id,)).fetchall()
    payments = conn.execute('SELECT * FROM payments WHERE customer_id = ? ORDER BY transaction_date DESC', (customer_id,)).fetchall()
    expenses = conn.execute('SELECT * FROM expenses WHERE customer_id = ? ORDER BY created_at DESC', (customer_id,)).fetchall()
    food_orders = conn.execute('SELECT * FROM food_orders WHERE customer_id = ? ORDER BY order_date DESC', (customer_id,)).fetchall()
    service_requests = conn.execute('SELECT * FROM service_requests WHERE customer_id = ? ORDER BY created_at DESC', (customer_id,)).fetchall()
    conn.close()

    exp_by_bid = {}
    for e in [dict(r) for r in expenses]:
        exp_by_bid.setdefault(e['booking_id'], []).append(e)

    pmt_by_bid = {}
    for p in [dict(r) for r in payments]:
        pmt_by_bid.setdefault(p['booking_id'], []).append(p)

    bookings = []
    total_lifetime = 0.0
    total_unpaid = 0.0
    for b in bookings_raw:
        b_dict = dict(b)
        bid = b_dict['id']
        b_fin = calculate_booking_financials(b_dict, exp_by_bid.get(bid, []), pmt_by_bid.get(bid, []))
        b_dict['financials'] = b_fin
        total_lifetime += b_fin['total_charges']
        if b_fin['outstanding_balance'] > 0:
            total_unpaid += b_fin['outstanding_balance']
        bookings.append(b_dict)

    return render_template(
        'admin/customer_detail.html',
        customer=customer,
        bookings=bookings,
        payments=payments,
        expenses=expenses,
        food_orders=food_orders,
        service_requests=service_requests,
        total_lifetime_fmt=format_inr(total_lifetime),
        total_unpaid_fmt=format_inr(total_unpaid)
    )

@app.route('/admin/revenue-analytics')
@admin_required
def admin_revenue_analytics():

    period = request.args.get('period', '30')
    period_days = 30
    if period == 'today':
        period_days = 1
    elif period == '7':
        period_days = 7
    elif period == '30':
        period_days = 30
    elif period == '90':
        period_days = 90

    conn = get_db_connection()
    bookings = [dict(r) for r in conn.execute('SELECT * FROM bookings').fetchall()]
    expenses = [dict(r) for r in conn.execute('SELECT * FROM expenses').fetchall()]
    payments = [dict(r) for r in conn.execute('SELECT * FROM payments').fetchall()]
    prooms = [dict(r) for r in conn.execute('SELECT * FROM physical_rooms').fetchall()]
    food_orders = [dict(r) for r in conn.execute('SELECT * FROM food_orders').fetchall()]
    service_reqs = [dict(r) for r in conn.execute('SELECT * FROM service_requests').fetchall()]
    conn.close()

    metrics = calculate_kpi_metrics(
        all_rooms_count=len(prooms),
        all_bookings=bookings,
        all_expenses=expenses,
        all_payments=payments,
        physical_rooms=prooms,
        food_orders=food_orders,
        service_requests=service_reqs,
        period_days=period_days
    )

    # Chart 1: Payments Collected Over Time (by transaction date)
    payments_by_date = {}
    for p in payments:
        if (p.get('transaction_status') or 'Success').capitalize() == 'Success':
            d_str = str(p.get('transaction_date'))[:10]
            payments_by_date[d_str] = payments_by_date.get(d_str, 0.0) + float(p.get('amount') or 0.0)

    # Chart 2: Revenue by Category
    category_revenue = {
        'Room Accommodation': metrics['room_revenue'],
        'Food & Beverage Dining': metrics['dining_revenue'],
        'Services & Laundry': metrics['services_revenue']
    }

    # Chart 3: Food Order Lifecycle Distribution
    order_status_dist = {}
    for fo in food_orders:
        st = fo.get('order_status', 'Placed')
        order_status_dist[st] = order_status_dist.get(st, 0) + 1

    # Chart 4: Physical Room Status Fleet Distribution
    room_status_dist = {
        'Available': metrics['available_rooms_count'],
        'Occupied': metrics['occupied_rooms_count'],
        'Cleaning': metrics['cleaning_rooms_count'],
        'Maintenance': metrics['maintenance_rooms_count']
    }

    return render_template(
        'admin/revenue_analytics.html',
        metrics=metrics,
        period=period,
        payments_by_date=payments_by_date,
        category_revenue=category_revenue,
        order_status_dist=order_status_dist,
        room_status_dist=room_status_dist
    )

@app.route('/admin/inquiries')
@admin_required
def admin_inquiries():
    conn = get_db_connection()
    inquiries = conn.execute('SELECT * FROM inquiries ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('admin/inquiries.html', inquiries=inquiries)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
