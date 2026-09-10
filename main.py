import os
import time
from datetime import datetime
import pytz
import requests
from threading import Thread
from http.server import SimpleHTTPRequestHandler, HTTPServer

# 1. Fake Web Server to satisfy Render web checks
def run_fake_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHTTPRequestHandler)
    server.serve_forever()

Thread(target=run_fake_server, daemon=True).start()

# 2. IMMEDIATE TEST MESSAGE TO DISCORD
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

test_payload = {
    "content": "🏁 **FINAL SCORE (TEST)** 🏁\nCHW 5 @ NYY 2\nYour 24/7 cloud server is officially working and connected!"
}

print("Firing test message to Discord...")
try:
    response = requests.post(DISCORD_WEBHOOK_URL, json=test_payload)
    print(f"Discord response code: {response.status_code}")
except Exception as e:
    print(f"Failed to reach Discord: {e}")

# Keep app alive
while True:
    time.sleep(60)
