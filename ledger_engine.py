"""
Centralized Financial Engine, Expense Ledger, and Payment Calculations for RETREAT Hotel & Suites.
Designed for clean separation of financial logic and effortless transition to FastAPI + PostgreSQL.
"""

from typing import Dict, List, Any, Optional

def to_paise(val: float) -> int:
    """Converts a floating-point INR rupee amount into an exact integer paise to prevent precision errors."""
    return int(round(float(val) * 100))

def from_paise(paise: int) -> float:
    """Converts integer paise back to standard rupee float."""
    return round(paise / 100.0, 2)

def format_inr(value: float) -> str:
    """Format any numeric value into Indian Numbering System currency (₹)."""
    try:
        val = int(round(float(value)))
        s = str(abs(val))
        if len(s) <= 3:
            formatted = s
        else:
            last3 = s[-3:]
            rest = s[:-3]
            chunks = []
            while len(rest) > 2:
                chunks.append(rest[-2:])
                rest = rest[:-2]
            if rest:
                chunks.append(rest)
            chunks.reverse()
            formatted = ','.join(chunks) + ',' + last3
        prefix = '-' if val < 0 else ''
        return f"₹{prefix}{formatted}"
    except (ValueError, TypeError):
        return f"₹{value}"

def calculate_booking_financials(
    booking_dict: Dict[str, Any],
    expenses_list: List[Dict[str, Any]],
    payments_list: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Centralized financial calculation following strict hotel accounting rules:
    
    Total Charges =
      Room Charges
      + Meal Plan Charges
      + Finalized Additional Expenses (Delivered room service, laundry, extra bedding, etc.)
    
    Outstanding Balance =
      Total Charges
      - Successful Payments
      - Approved Adjustments
      
    Excluded:
      - Cancelled food orders / expenses
      - Pending / Placed / Preparing food orders (not yet finalized)
      - Failed payment transactions
    """
    # 1. Base charges from booking
    base_room_paise = to_paise(booking_dict.get('base_room_charges') or booking_dict.get('total_price') or 0.0)
    meal_plan_paise = to_paise(booking_dict.get('meal_plan_charge') or 0.0)
    extra_guest_paise = to_paise(booking_dict.get('extra_guest_charges') or 0.0)
    discount_paise = to_paise(booking_dict.get('discount_amount') or 0.0)

    # 2. Categorized Finalized Expenses
    # Only expenses with status 'Finalized' count as enforceable charges
    expense_breakdown = {
        'Room Charges': 0,
        'Meal Plan': 0,
        'Food & Beverages': 0,
        'Room Service': 0,
        'Laundry': 0,
        'Additional Services': 0,
        'Other Charges': 0
    }
    
    additional_expenses_paise = 0
    finalized_expenses_list = []

    has_room_charges_exp = False
    has_meal_plan_exp = False

    for exp in expenses_list:
        status = (exp.get('expense_status') or 'Finalized').strip().capitalize()
        if status != 'Finalized':
            continue

        cat = exp.get('category') or 'Other Charges'
        amt_paise = to_paise(exp.get('total_amount') or 0.0)

        if cat == 'Room Charges':
            has_room_charges_exp = True
        elif cat == 'Meal Plan':
            has_meal_plan_exp = True

        # Standardize category
        if cat in expense_breakdown:
            expense_breakdown[cat] += amt_paise
        else:
            expense_breakdown['Other Charges'] += amt_paise

        # If it's an incidental or order (not initial room charge or meal plan bundle)
        if cat not in ['Room Charges', 'Meal Plan']:
            additional_expenses_paise += amt_paise

        finalized_expenses_list.append(dict(exp))

    # If expenses list doesn't already contain explicit Room Charges, synthesize and include it
    if not has_room_charges_exp:
        rc_paise = base_room_paise + extra_guest_paise
        expense_breakdown['Room Charges'] = rc_paise
        if rc_paise > 0:
            b_id = booking_dict.get('id') or 0
            nights = booking_dict.get('total_nights') or 1
            rooms_count = booking_dict.get('rooms_count') or 1
            room_name = booking_dict.get('room_name') or 'Accommodation'
            synthesized_rc = {
                'id': f"synth-rc-{b_id}",
                'expense_ref': f"EXP-RC-{b_id:04d}",
                'booking_id': b_id,
                'customer_id': booking_dict.get('customer_id', ''),
                'category': 'Room Charges',
                'description': f"{room_name} ({nights} Night{'s' if nights > 1 else ''}, {rooms_count} Room)",
                'quantity': nights,
                'unit_price': round(from_paise(rc_paise) / max(1, nights), 2),
                'total_amount': from_paise(rc_paise),
                'expense_status': 'Finalized',
                'created_at': booking_dict.get('created_at', '')
            }
            # Place room charges first
            finalized_expenses_list.insert(0, synthesized_rc)

    # If expenses list doesn't already contain explicit Meal Plan, synthesize and include it
    if not has_meal_plan_exp:
        expense_breakdown['Meal Plan'] = meal_plan_paise
        if meal_plan_paise > 0:
            b_id = booking_dict.get('id') or 0
            mp_name = booking_dict.get('meal_plan') or 'Selected Meal Plan'
            synthesized_mp = {
                'id': f"synth-mp-{b_id}",
                'expense_ref': f"EXP-MP-{b_id:04d}",
                'booking_id': b_id,
                'customer_id': booking_dict.get('customer_id', ''),
                'category': 'Meal Plan',
                'description': f"Meal Plan: {mp_name}",
                'quantity': 1,
                'unit_price': from_paise(meal_plan_paise),
                'total_amount': from_paise(meal_plan_paise),
                'expense_status': 'Finalized',
                'created_at': booking_dict.get('created_at', '')
            }
            # Insert meal plan right after room charges if present
            insert_idx = 1 if len(finalized_expenses_list) > 0 and finalized_expenses_list[0].get('category') == 'Room Charges' else 0
            finalized_expenses_list.insert(insert_idx, synthesized_mp)

    total_charges_paise = sum(expense_breakdown.values()) - discount_paise
    total_charges_paise = max(0, total_charges_paise)

    # 3. Successful Payments Calculation
    total_paid_paise = 0
    successful_payments_list = []

    for p in payments_list:
        p_status = (p.get('transaction_status') or 'Success').strip().capitalize()
        if p_status == 'Success':
            p_amt = to_paise(p.get('amount') or 0.0)
            total_paid_paise += p_amt
            successful_payments_list.append(p)

    # 4. Outstanding Balance & Payment Status
    outstanding_paise = total_charges_paise - total_paid_paise
    
    if total_paid_paise == 0:
        payment_status = 'Unpaid'
    elif outstanding_paise <= 0:
        payment_status = 'Paid'
    else:
        payment_status = 'Partially Paid'

    total_charges = from_paise(total_charges_paise)
    total_paid = from_paise(total_paid_paise)
    outstanding_balance = from_paise(outstanding_paise)

    # Converted readable breakdown dictionary
    breakdown_formatted = {k: format_inr(from_paise(v)) for k, v in expense_breakdown.items()}
    breakdown_floats = {k: from_paise(v) for k, v in expense_breakdown.items()}

    room_charges_amt = breakdown_floats['Room Charges']
    meal_plan_amt = breakdown_floats['Meal Plan']

    return {
        'total_charges': total_charges,
        'total_charges_formatted': format_inr(total_charges),
        'total_paid': total_paid,
        'total_paid_formatted': format_inr(total_paid),
        'outstanding_balance': outstanding_balance,
        'outstanding_balance_formatted': format_inr(outstanding_balance),
        'payment_status': payment_status,
        'is_fully_paid': (outstanding_paise <= 0),
        'room_charges': room_charges_amt,
        'room_charges_formatted': format_inr(room_charges_amt),
        'meal_plan': meal_plan_amt,
        'meal_plan_formatted': format_inr(meal_plan_amt),
        'expense_breakdown': breakdown_floats,
        'expense_breakdown_fmt': breakdown_formatted,
        'additional_expenses_total': from_paise(additional_expenses_paise),
        'additional_expenses_total_formatted': format_inr(from_paise(additional_expenses_paise)),
        'finalized_expenses': finalized_expenses_list,
        'successful_payments': successful_payments_list
    }

def calculate_kpi_metrics(
    all_rooms_count: int,
    all_bookings: List[Dict[str, Any]],
    all_expenses: List[Dict[str, Any]],
    all_payments: List[Dict[str, Any]],
    physical_rooms: List[Dict[str, Any]],
    food_orders: List[Dict[str, Any]],
    service_requests: List[Dict[str, Any]],
    period_days: int = 30
) -> Dict[str, Any]:
    """
    Centralized, audit-compliant hospitality KPI & Revenue analytics engine.
    Calculates operational metrics, room states, and financial indicators:
      - Total Reservations
      - Currently Occupied Rooms
      - Available Rooms
      - Today's Check-ins
      - Today's Check-outs
      - Payments Collected (Sum of successful transactions)
      - Outstanding Guest Balances (Sum of positive unpaid balances across active folios)
      - Pending Food Orders
      - Total Finalized Charges (Billed charges excluding cancelled/pending)
      - Room Revenue (Recognized accommodation tariff)
      - Dining & Service Revenue (Delivered F&B and chargeable services)
      - Occupancy Rate (Occupied room-nights / Available room-nights)
      - ADR (Average Daily Rate = Room Revenue / Occupied Room-Nights)
      - RevPAR (Revenue Per Available Room = Room Revenue / Available Room-Nights)
    """
    from datetime import date, datetime

    today_str = date.today().strftime('%Y-%m-%d')

    # Total Reservations (non-cancelled)
    non_cancelled_bookings = [b for b in all_bookings if (b.get('status') or '').lower() != 'cancelled']
    total_reservations = len(non_cancelled_bookings)

    # Physical room occupancy states
    occupied_rooms_count = sum(1 for pr in physical_rooms if pr.get('operational_status') == 'Occupied')
    available_rooms_count = sum(1 for pr in physical_rooms if pr.get('operational_status') == 'Available')
    cleaning_rooms_count = sum(1 for pr in physical_rooms if pr.get('operational_status') == 'Cleaning')
    maintenance_rooms_count = sum(1 for pr in physical_rooms if pr.get('operational_status') in ['Maintenance', 'Out of Service'])

    # Today's Expected / Actual Check-ins & Check-outs
    today_checkins = sum(1 for b in non_cancelled_bookings if b.get('check_in') == today_str)
    today_checkouts = sum(1 for b in non_cancelled_bookings if b.get('check_out') == today_str)

    # Total Payments Collected (Successful payments)
    total_payments_collected = sum(
        p.get('amount', 0.0) for p in all_payments if (p.get('transaction_status') or 'Success').strip().capitalize() == 'Success'
    )

    # Pre-index expenses and payments per booking for exact ledger evaluation
    exp_by_booking: Dict[int, List[Dict[str, Any]]] = {}
    for exp in all_expenses:
        bid = exp.get('booking_id')
        if bid:
            exp_by_booking.setdefault(bid, []).append(exp)

    pmt_by_booking: Dict[int, List[Dict[str, Any]]] = {}
    for pmt in all_payments:
        bid = pmt.get('booking_id')
        if bid:
            pmt_by_booking.setdefault(bid, []).append(pmt)

    total_outstanding_balances = 0.0
    total_room_revenue = 0.0
    total_dining_revenue = 0.0
    total_services_revenue = 0.0
    total_finalized_charges = 0.0
    occupied_room_nights = 0

    for b in non_cancelled_bookings:
        b_id = b.get('id')
        b_fin = calculate_booking_financials(b, exp_by_booking.get(b_id, []), pmt_by_booking.get(b_id, []))
        
        # Only accumulate positive outstanding balances for active non-checked-out/unsettled bookings
        if b_fin['outstanding_balance'] > 0:
            total_outstanding_balances += b_fin['outstanding_balance']

        total_finalized_charges += b_fin['total_charges']
        total_room_revenue += b_fin['room_charges']
        
        # F&B and Services from breakdown
        total_dining_revenue += (b_fin['expense_breakdown'].get('Food & Beverages', 0.0) + b_fin['expense_breakdown'].get('Room Service', 0.0))
        total_services_revenue += (b_fin['expense_breakdown'].get('Laundry', 0.0) + b_fin['expense_breakdown'].get('Additional Services', 0.0) + b_fin['expense_breakdown'].get('Other Charges', 0.0))

        # Room nights calculation for non-cancelled bookings
        nights = int(b.get('total_nights') or 1)
        rooms_count = int(b.get('rooms_count') or 1)
        occupied_room_nights += (nights * rooms_count)

    # Pending food orders (Placed, Preparing, Ready, Out for Delivery)
    pending_food_orders = sum(1 for fo in food_orders if fo.get('order_status') not in ['Delivered', 'Cancelled'])

    # Pending service requests
    pending_service_requests = sum(1 for sr in service_requests if sr.get('status') in ['Pending', 'In Progress'])

    # Available room nights in the standard fleet over the reporting window
    total_fleet_rooms = len(physical_rooms) if physical_rooms else all_rooms_count
    available_room_nights = total_fleet_rooms * max(1, period_days)

    # Hospitality KPIs
    occupancy_pct = round((occupied_room_nights / available_room_nights * 100.0), 1) if available_room_nights > 0 else 0.0
    adr = round(total_room_revenue / occupied_room_nights, 2) if occupied_room_nights > 0 else 0.0
    revpar = round(total_room_revenue / available_room_nights, 2) if available_room_nights > 0 else 0.0

    return {
        'total_reservations': total_reservations,
        'occupied_rooms_count': occupied_rooms_count,
        'available_rooms_count': available_rooms_count,
        'cleaning_rooms_count': cleaning_rooms_count,
        'maintenance_rooms_count': maintenance_rooms_count,
        'today_checkins': today_checkins,
        'today_checkouts': today_checkouts,
        'total_payments_collected': round(total_payments_collected, 2),
        'total_payments_collected_fmt': format_inr(total_payments_collected),
        'total_outstanding_balances': round(total_outstanding_balances, 2),
        'total_outstanding_balances_fmt': format_inr(total_outstanding_balances),
        'pending_food_orders': pending_food_orders,
        'pending_service_requests': pending_service_requests,
        'total_finalized_charges': round(total_finalized_charges, 2),
        'total_finalized_charges_fmt': format_inr(total_finalized_charges),
        'room_revenue': round(total_room_revenue, 2),
        'room_revenue_fmt': format_inr(total_room_revenue),
        'dining_revenue': round(total_dining_revenue, 2),
        'dining_revenue_fmt': format_inr(total_dining_revenue),
        'services_revenue': round(total_services_revenue, 2),
        'services_revenue_fmt': format_inr(total_services_revenue),
        'occupancy_rate_pct': occupancy_pct,
        'occupancy_rate_fmt': f"{occupancy_pct}%",
        'adr': adr,
        'adr_fmt': format_inr(adr),
        'revpar': revpar,
        'revpar_fmt': format_inr(revpar),
        'period_days': period_days,
        'occupied_room_nights': occupied_room_nights,
        'available_room_nights': available_room_nights
    }
