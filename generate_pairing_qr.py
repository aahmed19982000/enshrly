import socket
import json
import os

# 1. Detect local network IP address
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

local_ip = get_local_ip()
server_url = f"http://{local_ip}:8000/payments/api/v1/payments"

payload = {
    "pair_token": "enshrly_pairing_token_2026",
    "server_url": server_url
}

payload_str = json.dumps(payload)
print(f"Generated Payload: {payload_str}")

# Try to install qrcode if not installed
try:
    import qrcode
except ImportError:
    print("Installing qrcode package...")
    import subprocess
    subprocess.check_call([os.path.join(".venv", "Scripts", "pip.exe"), "install", "qrcode", "pillow"])
    import qrcode

qr = qrcode.QRCode(version=1, box_size=10, border=5)
qr.add_data(payload_str)
qr.make(fit=True)

img = qr.make_image(fill_color="black", back_color="white")
img.save("pairing_qr.png")

print(f"Success! QR Code saved as G:\\enshrly\\pairing_qr.png with URL: {server_url}")
