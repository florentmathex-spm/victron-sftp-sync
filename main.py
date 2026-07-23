import os
import csv
import requests
import paramiko
from datetime import datetime

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
VRM_API_TOKEN = os.getenv("VRM_API_TOKEN")
VRM_SITE_ID = os.getenv("VRM_SITE_ID")

SFTP_HOST = os.getenv("SFTP_HOST")
SFTP_PORT = int(os.getenv("SFTP_PORT", 22))
SFTP_USER = os.getenv("SFTP_USER")
SFTP_PASS = os.getenv("SFTP_PASS")
SFTP_REMOTE_PATH = os.getenv("SFTP_REMOTE_PATH", "solar_data.csv")

LOCAL_FILE_PATH = "solar_data.csv"

# ------------------------------------------------------------------
# 1. RÉCUPÉRATION DES DIAGNOSTICS VICTRON
# ------------------------------------------------------------------
def fetch_victron_diagnostics():
    url = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/diagnostics"
    headers = {
        "x-authorization": f"Token {VRM_API_TOKEN}",
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()

# ------------------------------------------------------------------
# 2. FONCTION DE NETTOYAGE NUMÉRIQUE
# ------------------------------------------------------------------
def parse_number(val_str):
    if val_str is None:
        return ""
    try:
        # Prend le premier mot/élément numérique (ex: "230.1 V" -> 230.1)
        s = str(val_str).strip().split()[0].replace(',', '.')
        return float(s)
    except (ValueError, TypeError, IndexError):
        return ""

# ------------------------------------------------------------------
# 3. PARSER ROBUSTE GROUPÉ PAR INSTANCE
# ------------------------------------------------------------------
def parse_and_format_s4e(diagnostics_data, output_path):
    records = diagnostics_data.get("records", [])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Dictionnaire temporaire pour stocker les mesures reçues par instance
    # instance_id -> {"type": ..., "serial": ..., "metrics": {...}}
    instances = {}

    for rec in records:
        code = str(rec.get("code", ""))
        desc = str(rec.get("description", "")).lower()
        val = rec.get("formattedValue", rec.get("rawValue", ""))
        num_val = parse_number(val)

        # Instance de l'appareil (ex: 277, 278, 512, 276)
        instance_id = str(rec.get("instance", rec.get("idSite", VRM_SITE_ID)))

        if instance_id not in instances:
            instances[instance_id] = {
                "type": "unknown",
                "serial": f"dev_{instance_id}",
                "metrics": {}
            }

        inst = instances[instance_id]

        # --- DÉTECTION DU TYPE D'ÉQUIPEMENT ET NUMÉRO DE SÉRIE ---
        if code == "ScSN":
            inst["type"] = "mppt"
            inst["serial"] = str(val)
        elif code == "BM" or "battery" in desc or code in ["SOC", "BT", "ca"]:
            inst["type"] = "battery"
            if inst["serial"].startswith("dev_"):
                inst["serial"] = f"batt_{instance_id}"
        elif "ve.bus" in desc or code in ["OP1", "OV1", "OI1", "IP1", "IV1", "II1"]:
            inst["type"] = "converter"
            if inst["serial"].startswith("dev_"):
                inst["serial"] = f"inv_{instance_id}"
        elif "solar charger" in desc or code in ["PVP", "PVV", "ScI", "ScV", "ScW", "YT"]:
            inst["type"] = "mppt"
            if inst["serial"].startswith("dev_"):
                inst["serial"] = f"mppt_{instance_id}"

        # --- EXTRACTION DES MÉTRIQUES SELON LE CODE ---
        # A. MPPT / Solaire
        if code in ["PVP", "PVP0", "ScW"]:               # Puissance Solaire (W)
            inst["metrics"]["power"] = num_val
        elif code in ["PVV", "PVV0", "ScV"]:              # Tension Solaire (V)
            inst["metrics"]["volt"] = num_val
        elif code in ["ScI"]:                            # Courant Solaire (A)
            inst["metrics"]["current"] = num_val
        elif code in ["YT"]:                             # Production jour (kWh)
            inst["metrics"]["energy"] = num_val

        # B. Batterie / BMS
        elif code in ["SOC", "bs"]:                      # SOC (%)
            inst["metrics"]["state_of_charge"] = num_val
        elif code in ["V", "bv"]:                        # Tension Batterie (V)
            inst["metrics"]["volt"] = num_val
        elif code in ["I", "bc"]:                        # Courant Batterie (A)
            inst["metrics"]["current"] = num_val
        elif code in ["BT", "bT", "CT"]:                 # Température Batterie (°C)
            inst["metrics"]["temperature"] = num_val
        elif code in ["ca"]:                             # Capacité Batterie (Ah)
            inst["metrics"]["capacity"] = num_val
        elif code in ["BP", "bp"]:                       # Puissance Batterie (W)
            inst["metrics"]["power"] = num_val

        # C. Onduleur / Convertisseur
        elif code in ["OP1", "o1"]:                      # Puissance Sortie AC (W)
            inst["metrics"]["power"] = num_val
        elif code in ["OV1"]:                            # Tension Sortie AC (V)
            inst["metrics"]["volt"] = num_val
        elif code in ["OI1"]:                            # Courant Sortie AC (A)
            inst["metrics"]["current"] = num_val
        elif code in ["IP1"]:                            # Puissance Entrée AC / Réseau (W)
            inst["metrics"]["power_in"] = num_val
        elif code in ["IV1"]:                            # Tension Entrée AC / Réseau (V)
            inst["metrics"]["volt_in"] = num_val
        elif code in ["II1"]:                            # Courant Entrée AC / Réseau (A)
            inst["metrics"]["current_in"] = num_val
        elif code in ["t9"]:                             # Énergie totale produite (kWh)
            inst["metrics"]["energy_tot"] = num_val

    # --- STRUCTURE CSV POWER API S4E ---
    headers = [
        "date", "device", "serial", 
        "power", "volt", "current", "energy", "energy_tot",
        "power_in", "volt_in", "current_in", 
        "state_of_charge", "temperature", "capacity"
    ]

    rows = []
    for inst_id, item in instances.items():
        dev_type = item["type"]
        
        # On ne garde que les vrais appareils (MPPT, Battery, Converter)
        if dev_type not in ["mppt", "battery", "converter"]:
            continue

        metrics = item["metrics"]
        
        # On vérifie qu'au moins une donnée numérique existe
        if not any(v != "" for v in metrics.values()):
            continue

        rows.append({
            "date": now_str,
            "device": dev_type,
            "serial": item["serial"],
            "power": metrics.get("power", ""),
            "volt": metrics.get("volt", ""),
            "current": metrics.get("current", ""),
            "energy": metrics.get("energy", ""),
            "energy_tot": metrics.get("energy_tot", ""),
            "power_in": metrics.get("power_in", ""),
            "volt_in": metrics.get("volt_in", ""),
            "current_in": metrics.get("current_in", ""),
            "state_of_charge": metrics.get("state_of_charge", ""),
            "temperature": metrics.get("temperature", ""),
            "capacity": metrics.get("capacity", "")
        })

    # Écriture dans le fichier CSV
    with open(output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f" CSV Power API généré avec succès ({len(rows)} appareils remplis).")

# ------------------------------------------------------------------
# 4. TRANSFERT SFTP
# ------------------------------------------------------------------
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

# ------------------------------------------------------------------
# DÉCLENCHEMENT
# ------------------------------------------------------------------
if __name__ == "__main__":
    try:
        data = fetch_victron_diagnostics()
        parse_and_format_s4e(data, LOCAL_FILE_PATH)
        upload_via_sftp(LOCAL_FILE_PATH, SFTP_REMOTE_PATH)
    except Exception as e:
        print(f" Erreur : {e}")
        exit(1)
