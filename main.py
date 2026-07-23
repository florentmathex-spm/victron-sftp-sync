import os
import csv
import requests
import paramiko
from datetime import datetime

VRM_API_TOKEN = os.getenv("VRM_API_TOKEN")
VRM_SITE_ID = os.getenv("VRM_SITE_ID")

SFTP_HOST = os.getenv("SFTP_HOST")
SFTP_PORT = int(os.getenv("SFTP_PORT", 22))
SFTP_USER = os.getenv("SFTP_USER")
SFTP_PASS = os.getenv("SFTP_PASS")
SFTP_REMOTE_PATH = os.getenv("SFTP_REMOTE_PATH", "solar_data.csv")
LOCAL_FILE_PATH = "solar_data.csv"

def fetch_victron_diagnostics():
    url = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/diagnostics"
    headers = {
        "x-authorization": f"Token {VRM_API_TOKEN}",
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()

def parse_and_format_s4e(diagnostics_data, output_path):
    records = diagnostics_data.get("records", [])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"--- [DEBUG] Nombre total de registres reçus de Victron : {len(records)} ---")
    
    devices_data = {}

    for rec in records:
        # Affichage pour voir ce qui arrive dans les logs GitHub Actions
        desc = str(rec.get("description", ""))
        code = str(rec.get("code", ""))
        val = rec.get("formattedValue", rec.get("rawValue", ""))
        print(f"Registre -> Description: '{desc}' | Code: '{code}' | Valeur: '{val}'")

        desc_lower = desc.lower()
        device_id = str(rec.get("instance", rec.get("idSite", VRM_SITE_ID)))

        # Logique d'attribution élargie
        if any(k in desc_lower for k in ["mppt", "solar", "charger", "pv"]):
            key = ("mppt", f"mppt_{device_id}")
        elif any(k in desc_lower for k in ["vebus", "inverter", "multi", "quattro", "converter"]):
            key = ("converter", f"inv_{device_id}")
        elif any(k in desc_lower for k in ["battery", "bms", "bmv", "soc"]):
            key = ("battery", f"batt_{device_id}")
        else:
            # Par défaut, on rattche au site global si non classé
            key = ("site", VRM_SITE_ID)

        if key not in devices_data:
            devices_data[key] = {}

        # Mapping large des codes fréquents chez Victron
        code_upper = code.upper()
        if code_upper in ["P", "PPV", "PVPOWER", "POWER"]:
            devices_data[key]["power"] = val
        elif code_upper in ["V", "PVV", "VOLT", "VOLTAGE"]:
            devices_data[key]["volt"] = val
        elif code_upper in ["I", "PVI", "CURRENT"]:
            devices_data[key]["current"] = val
        elif code_upper in ["SOC", "STATEOFCHARGE"]:
            devices_data[key]["state_of_charge"] = val
        elif code_upper in ["T", "TEMP", "TEMPERATURE"]:
            devices_data[key]["temperature"] = val

    headers = [
        "date", "device", "serial", 
        "power", "volt", "current", 
        "power_in", "volt_in", "current_in", 
        "state_of_charge", "temperature"
    ]

    rows = []
    for (device_type, serial), metrics in devices_data.items():
        rows.append({
            "date": now_str,
            "device": device_type,
            "serial": serial,
            "power": metrics.get("power", ""),
            "volt": metrics.get("volt", ""),
            "current": metrics.get("current", ""),
            "power_in": metrics.get("power_in", ""),
            "volt_in": metrics.get("volt_in", ""),
            "current_in": metrics.get("current_in", ""),
            "state_of_charge": metrics.get("state_of_charge", ""),
            "temperature": metrics.get("temperature", "")
        })

    # Fallback si vraiment vide
    if not rows:
        rows.append({
            "date": now_str, "device": "site", "serial": VRM_SITE_ID,
            "power": "", "volt": "", "current": "", "power_in": "", 
            "volt_in": "", "current_in": "", "state_of_charge": "", "temperature": ""
        })

    with open(output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f" Fichier CSV généré avec {len(rows)} ligne(s).")

def upload_via_sftp(local_path, remote_path):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, password=SFTP_PASS, timeout=10)
    sftp = ssh.open_sftp()
    clean_path = remote_path.lstrip('/') if not remote_path.startswith('./') else remote_path
    sftp.put(local_path, clean_path)
    sftp.close()
    ssh.close()
    print(" Transfert SFTP réussi !")

if __name__ == "__main__":
    data = fetch_victron_diagnostics()
    parse_and_format_s4e(data, LOCAL_FILE_PATH)
    upload_via_sftp(LOCAL_FILE_PATH, SFTP_REMOTE_PATH)
