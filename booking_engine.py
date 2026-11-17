"""
Centralized Room Data, Inventory, Availability Engine, and Pricing Engine for RETREAT Hotel & Suites.
Designed for clean separation of business logic and effortless transition to FastAPI + PostgreSQL.
"""

from datetime import datetime, date
from typing import Dict, List, Any, Optional

# Standard Inventory Configuration (Illustrative prototype values)
INVENTORY_DEFAULTS = {
    1: 10,  # Comfort Room
    2: 8,   # Canopy Deluxe Room
    3: 5,   # Executive Suite
    4: 3    # Premium Family Suite
}

# Optional Meal Plan Configurations
MEAL_PLANS = {
    'room_only': {
        'id': 'room_only',
        'name': 'Room Only',
        'rate_per_adult_per_night': 0,
        'description': 'Pure room stay. Food & beverages billed separately.'
    },
    'bed_breakfast': {
        'id': 'bed_breakfast',
        'name': 'Bed & Breakfast',
        'rate_per_adult_per_night': 500,
        'description': 'Daily complimentary breakfast buffet at The Retreat Café (₹500 / adult / night).'
    },
    'half_board': {
        'id': 'half_board',
        'name': 'Half Board (Breakfast + Dinner)',
        'rate_per_adult_per_night': 900,
        'description': 'Breakfast buffet plus curated multi-course dinner (₹900 / adult / night).'
    },
    'full_board': {
        'id': 'full_board',
        'name': 'Full Board (All Meals)',
        'rate_per_adult_per_night': 1300,
        'description': 'Breakfast, lunch, and dinner buffet included daily (₹1,300 / adult / night).'
    }
}

# Hotel Stay Policies
HOTEL_POLICIES = {
    'check_in_time': '2:00 PM',
    'check_out_time': '11:00 AM',
    'early_check_in': 'Subject to availability upon arrival; early morning arrivals before 8 AM billed 50% day rate.',
    'late_check_out': 'Complimentary up to 12:00 PM upon request; late departures up to 6:00 PM billed 50% day rate.',
    'cancellation_policy': 'Free cancellation up to 48 hours prior to check-in date. Cancellations made within 48 hours incur 1 night room charge.',
    'child_policy': 'Children up to 6 years stay complimentary sharing existing bedding. Children 7+ count as standard occupancy.',
    'id_requirement': 'Government-issued photo identification (Aadhaar, Passport, Voter ID, or Driving License) required at check-in for all adult guests.'
}

def calculate_cancellation_and_refund(
    booking: Dict[str, Any],
    total_paid: float,
    completed_services_charge: float = 0.0,
    previous_refunds: float = 0.0,
    as_of_date: Optional[date] = None
) -> Dict[str, Any]:
    """
    Centralized cancellation and refund calculation logic following realistic hotel policies:
    
    1. Before free-cancellation deadline (>48h prior to check-in):
       - Full refund of eligible accommodation payments.
       - Penalty = 0.
       - Eligible charges = Completed services (if any).
    
    2. After free-cancellation deadline (within 48h prior to check-in):
       - Penalty = 1 night's base room tariff.
       - Eligible charges = Completed services.
       - Net Refund = Total Paid - Eligible Charges - Penalty - Previous Refunds (minimum 0).
    
    3. After check-in (Early departure / In-house cancellation):
       - Occupied nights charged (at least 1 night or actual days between check-in and cancellation).
       - Completed food orders and guest services kept as legitimate charges.
       - Penalty = 0 (nights used are charged).
       - Net Refund = Total Paid - Consumed Room Charges - Completed Services - Previous Refunds (minimum 0).
       
    4. General rule:
       Refund Amount = max(0.0, min(total_paid - previous_refunds, total_paid - eligible_charges - penalty - previous_refunds))
    """
    if as_of_date is None:
        as_of_date = date.today()

    check_in_date = datetime.strptime(str(booking['check_in'])[:10], '%Y-%m-%d').date()
    check_out_date = datetime.strptime(str(booking['check_out'])[:10], '%Y-%m-%d').date()
    total_nights = max(1, int(booking.get('total_nights') or (check_out_date - check_in_date).days or 1))
    total_price = float(booking.get('total_price') or 0.0)
    per_night_rate = round(total_price / total_nights, 2)
    booking_status = booking.get('status', 'Confirmed')

    # Deadline: 2 days (48 hours) before check_in
    days_to_checkin = (check_in_date - as_of_date).days

    cancellation_tier = ""
    cancellation_penalty = 0.0
    consumed_room_charges = 0.0
    policy_description = ""

    if booking_status in ['Checked In']:
        # Early departure / In-house cancellation
        cancellation_tier = "early_departure"
        # Determine nights stayed
        actual_in = booking.get('actual_check_in')
        if actual_in:
            try:
                actual_in_date = datetime.strptime(str(actual_in)[:10], '%Y-%m-%d').date()
                nights_stayed = max(1, (as_of_date - actual_in_date).days)
            except Exception:
                nights_stayed = 1
        else:
            nights_stayed = max(1, (as_of_date - check_in_date).days if as_of_date >= check_in_date else 1)
        
        nights_stayed = min(total_nights, nights_stayed)
        consumed_room_charges = round(nights_stayed * per_night_rate, 2)
        cancellation_penalty = 0.0
        policy_description = f"Early departure: {nights_stayed} night(s) charged at {format_inr(per_night_rate)}/night."

    elif days_to_checkin >= 2:
        # Free cancellation window (>= 48 hours prior)
        cancellation_tier = "free_cancellation"
        cancellation_penalty = 0.0
        consumed_room_charges = 0.0
        policy_description = "Full refund eligible (cancelled >48h before check-in, no penalty)."

    elif days_to_checkin >= 0:
        # Within 48 hours prior to check-in
        cancellation_tier = "late_cancellation_penalty"
        cancellation_penalty = round(min(total_price, per_night_rate), 2)
        consumed_room_charges = 0.0
        policy_description = f"Late cancellation within 48 hours: 1 night room charge penalty ({format_inr(cancellation_penalty)})."

    else:
        # No-show (check-in date already passed without check-in)
        cancellation_tier = "no_show"
        cancellation_penalty = round(min(total_price, per_night_rate * min(2, total_nights)), 2)
        consumed_room_charges = 0.0
        policy_description = f"No-show / Past arrival cancellation: retention fee of {format_inr(cancellation_penalty)} applied."

    total_eligible_charges = round(consumed_room_charges + completed_services_charge, 2)
    
    # Net refund formula: Amount Paid - Eligible Charges - Cancellation Penalty - Previous Refunds
    calculated_refund = total_paid - total_eligible_charges - cancellation_penalty - previous_refunds
    refundable_amount = round(max(0.0, calculated_refund), 2)
    # Cannot refund more than total_paid - previous_refunds
    refundable_amount = min(refundable_amount, max(0.0, total_paid - previous_refunds))

    # Refund status determination
    if total_paid <= 0:
        refund_status = "Refund Not Applicable"
    elif refundable_amount > 0:
        refund_status = "Refund Pending"
    else:
        refund_status = "No Refund Due (Charges exceed payments)"

    return {
        'cancellation_tier': cancellation_tier,
        'policy_description': policy_description,
        'total_paid': total_paid,
        'total_paid_fmt': format_inr(total_paid),
        'consumed_room_charges': consumed_room_charges,
        'consumed_room_charges_fmt': format_inr(consumed_room_charges),
        'completed_services_charge': completed_services_charge,
        'completed_services_charge_fmt': format_inr(completed_services_charge),
        'total_eligible_charges': total_eligible_charges,
        'total_eligible_charges_fmt': format_inr(total_eligible_charges),
        'cancellation_penalty': cancellation_penalty,
        'cancellation_penalty_fmt': format_inr(cancellation_penalty),
        'previous_refunds': previous_refunds,
        'previous_refunds_fmt': format_inr(previous_refunds),
        'refundable_amount': refundable_amount,
        'refundable_amount_fmt': format_inr(refundable_amount),
        'refund_status': refund_status
    }

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

def calculate_nights(check_in_str: str, check_out_str: str) -> int:
    """Inclusive check-in, exclusive check-out standard hotel night calculator."""
    d1 = datetime.strptime(check_in_str, '%Y-%m-%d').date()
    d2 = datetime.strptime(check_out_str, '%Y-%m-%d').date()
    diff = (d2 - d1).days
    return diff

def validate_dates(check_in_str: str, check_out_str: str) -> Optional[str]:
    """Validate that check-in is not in the past and check-out is after check-in."""
    try:
        today = date.today()
        d1 = datetime.strptime(check_in_str, '%Y-%m-%d').date()
        d2 = datetime.strptime(check_out_str, '%Y-%m-%d').date()
    except Exception:
        return "Invalid date format provided. Please use YYYY-MM-DD."

    if d1 < today:
        return "Check-in date cannot be in the past."
    if d2 <= d1:
        return "Check-out date must be at least one day after check-in."
    return None

def calculate_availability_for_room(
    room_id: int,
    total_inventory: int,
    check_in_str: str,
    check_out_str: str,
    active_reservations: List[Dict[str, Any]]
) -> int:
    """
    Standard date-overlap condition:
    Requested check-in < Existing check-out AND Requested check-out > Existing check-in
    Allows back-to-back bookings (e.g., checkout on Nov 12 allows new checkin on Nov 12).
    Cancelled reservations DO NOT consume inventory.
    Returns: remaining available rooms count for the requested dates.
    """
    req_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
    req_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()

    consumed_rooms = 0
    for res in active_reservations:
        if res.get('room_id') != room_id:
            continue
        status = (res.get('status') or '').strip().lower()
        if status in ['cancelled', 'completed']:
            continue
        
        res_in = datetime.strptime(res['check_in'], '%Y-%m-%d').date()
        res_out = datetime.strptime(res['check_out'], '%Y-%m-%d').date()

        # Date overlap check: (req_in < res_out) and (req_out > res_in)
        if req_in < res_out and req_out > res_in:
            consumed_rooms += int(res.get('rooms_count') or 1)

    available = max(0, total_inventory - consumed_rooms)
    return available

def is_physical_room_available(
    room_number: str,
    check_in_str: str,
    check_out_str: str,
    active_assignments: List[Dict[str, Any]],
    current_operational_status: str = 'Available',
    exclude_booking_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Validates whether a specific physical room can be assigned for a stay period:
    1. Operational status must not be 'Maintenance' or 'Out of Service'.
       (Cleaning is permitted for future bookings, but flagged if check-in is today).
    2. No overlapping active reservations (status != 'Cancelled' and != 'Completed' and != 'Checked Out')
       with the same assigned room number.
    Returns: {'available': bool, 'reason': Optional[str]}
    """
    if current_operational_status in ['Maintenance', 'Out of Service']:
        return {
            'available': False,
            'reason': f"Room {room_number} is currently marked as '{current_operational_status}' and cannot be assigned."
        }

    try:
        req_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
        req_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()
    except Exception:
        return {'available': False, 'reason': 'Invalid dates.'}

    for b in active_assignments:
        if b.get('assigned_room_number') != room_number:
            continue
        if exclude_booking_id and b.get('id') == exclude_booking_id:
            continue
        status = (b.get('status') or '').strip().lower()
        if status in ['cancelled', 'completed', 'checked out']:
            continue

        try:
            res_in = datetime.strptime(b['check_in'], '%Y-%m-%d').date()
            res_out = datetime.strptime(b['check_out'], '%Y-%m-%d').date()
        except Exception:
            continue

        # Standard date overlap: req_in < res_out and req_out > res_in
        if req_in < res_out and req_out > res_in:
            return {
                'available': False,
                'reason': f"Room {room_number} is already reserved by {b.get('booking_code')} ({b.get('guest_name')}) from {b.get('check_in')} to {b.get('check_out')}."
            }

    return {'available': True, 'reason': None}

def calculate_pricing(
    base_price_per_night: float,
    additional_guest_charge: float,
    capacity_adults: int,
    max_occupancy: int,
    nights: int,
    rooms_count: int,
    adults: int,
    children: int,
    meal_plan_id: str = 'room_only',
    discount_percent: float = 0.0
) -> Dict[str, Any]:
    """
    Centralized pricing calculation utility:
    - Base room charges = base_price * nights * rooms_count
    - Additional guest charges = additional_guest_charge * extra_guests * nights
    - Optional meal plan charges = rate_per_adult * adults * nights
    - Discount deductions
    - Net total
    """
    nights = max(1, nights)
    rooms_count = max(1, rooms_count)

    base_room_charges = base_price_per_night * nights * rooms_count

    # Extra guests calculation across the booked rooms
    # Total standard capacity for rooms_count
    total_standard_adults = capacity_adults * rooms_count
    extra_adults = max(0, adults - total_standard_adults)

    extra_guest_charges = (additional_guest_charge or 0.0) * extra_adults * nights

    # Meal plan
    meal_plan_info = MEAL_PLANS.get(meal_plan_id, MEAL_PLANS['room_only'])
    meal_rate = meal_plan_info['rate_per_adult_per_night']
    meal_plan_charges = meal_rate * adults * nights

    subtotal = base_room_charges + extra_guest_charges + meal_plan_charges
    discount_amount = round(subtotal * (discount_percent / 100.0), 2)
    total_amount = subtotal - discount_amount

    return {
        'nights': nights,
        'rooms_count': rooms_count,
        'adults': adults,
        'children': children,
        'base_price_per_night': base_price_per_night,
        'base_price_per_night_fmt': format_inr(base_price_per_night),
        'base_room_charges': base_room_charges,
        'base_room_charges_fmt': format_inr(base_room_charges),
        'extra_adults': extra_adults,
        'additional_guest_charge_unit': additional_guest_charge,
        'additional_guest_charges': extra_guest_charges,
        'additional_guest_charges_fmt': format_inr(extra_guest_charges),
        'meal_plan_id': meal_plan_id,
        'meal_plan_name': meal_plan_info['name'],
        'meal_plan_charges': meal_plan_charges,
        'meal_plan_charges_fmt': format_inr(meal_plan_charges),
        'discount_amount': discount_amount,
        'discount_amount_fmt': format_inr(discount_amount),
        'total_amount': total_amount,
        'total_amount_fmt': format_inr(total_amount)
    }

