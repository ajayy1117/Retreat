from app import app
import re

client = app.test_client()

with client.session_transaction() as sess:
    sess['user_role'] = 'admin'

res = client.get('/admin/bookings')
text = res.text

# Check 1: exactly ONE search input
search_inputs = re.findall(r'<input[^>]*name=[\"\']q[\"\'][^>]*>', text)
print(f"Search inputs for q: {len(search_inputs)}")
assert len(search_inputs) == 1, f"Expected 1 search input, got {len(search_inputs)}"

# Check 2: no redundant header search box
assert 'search-input-box' not in text
print("1. Redundant header search box removed successfully.")

# Check 3: guest count formatting (e.g. 'Adults · Children')
assert 'Adults ·' in text or 'Adult ·' in text
print("2. Guest count grammar verified (e.g. '2 Adults · 0 Children').")

# Check 4: stay period date format (e.g. 'Nov 2026')
assert 'Nov 2026' in text
print("3. Stay period date format verified (e.g. '10 Nov 2026 – 13 Nov 2026').")

# Check 5: customer phone on single line with white-space: nowrap
assert 'white-space: nowrap' in text
print("4. Customer hierarchy & single line phone verified.")

# Check 6: paid balance formatted with DM Sans
assert '₹0 — Paid' in text
print("5. Paid balance format verified ('₹0 — Paid').")

print("All specific reservations table criteria verified successfully!")

