"""Fetch the Nomba balance API docs page."""
import httpx, re

url = "https://developer.nomba.com/nomba-api-reference/accounts/fetch-sub-account-balance"
r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
print(f"Status: {r.status_code} | Size: {len(r.text)} bytes")

# Extract the endpoint and relevant info
text = r.text
# Look for GET/POST paths
for m in re.finditer(r'(GET|POST|PUT|DELETE)\s+(/\S+)', text, re.IGNORECASE):
    print(f"  {m.group(1)} {m.group(2)}")

# Print the full text for inspection
print("\n--- FULL PAGE ---")
print(text[:8000])
