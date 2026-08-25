import requests
import json
import time

BASE_URL = "http://localhost:8000/api"

print("--- 1. Health Check ---")
resp = requests.get(f"{BASE_URL}/health")
print(resp.json())

print("\n--- 2. Create Client ---")
resp = requests.post(f"{BASE_URL}/client/", json={"name": "Shroff Family Sync Test"})
client = resp.json()
client_id = client["id"]
print(f"Created client {client_id}: {client['name']}")

print("\n--- 3. Upload PDFs ---")
pdfs = [
    r"C:\Users\hiten\Downloads\Portfolio Appraisal.pdf",
    r"C:\Users\hiten\Downloads\Demat Holding Stmt_4707_13-07-26 16.17.pdf",
    r"C:\Users\hiten\Downloads\Demat Holding Stmt_1112_13-07-26 16.20.pdf"
]

for pdf in pdfs:
    print(f"Uploading {pdf}...")
    with open(pdf, "rb") as f:
        files = {"file": (pdf.split("\\")[-1], f, "application/pdf")}
        data = {"client_id": client_id}
        resp = requests.post(f"{BASE_URL}/upload/", files=files, data=data)
        try:
            print(resp.json())
        except:
            print(resp.text)
        
print("\n--- Waiting for Analysis Engine (10s) ---")
time.sleep(10)

print("\n--- 4. Fetch Overview (To see all accounts synced) ---")
resp = requests.get(f"{BASE_URL}/overview/{client_id}")
overview = resp.json()
print(f"Family Total Value: Rs {overview.get('total_value', 0):,.2f}")
print(f"Family Total Holdings: {sum(a.get('holding_count', 0) for a in overview.get('accounts', []))}")
print("\nBreakdown by PDF/Account:")
for a in overview.get('accounts', []):
    print(f" - {a['label']} ({a['type']}): {a['holding_count']} holdings, Value: Rs {a['value']:,.2f}")

print("\n--- 5. LLM Narratives Generated ---")
print("Overview Narrative:", overview.get("narrative", ""))


print("\n--- 6. Fetch Performance ---")
resp = requests.get(f"{BASE_URL}/performance/{client_id}")
print(json.dumps(resp.json(), indent=2)[:500] + "\n...")

print("\n--- 7. Fetch Insights ---")
resp = requests.get(f"{BASE_URL}/insights/{client_id}")
print(json.dumps(resp.json(), indent=2)[:500] + "\n...")
