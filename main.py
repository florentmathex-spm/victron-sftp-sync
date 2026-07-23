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
# RÉCUPÉRATION DES DIAGNOSTICS VICTRON
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
# CONVERSION DES VALEURS NUMÉRIQUES
# ------------------------------------------------------------------
def parse_number(val_str):
    if val_str is None:
        return ""
    try:
        # Extrait le premier élément numérique (ex: "230.1 V" -> 230.1)
        s = str(val_str).strip().split()[0].replace(',', '.')
        return float(s)
    except (ValueError, TypeError, IndexError):
        return ""

# ------------------------------------------------------------------
# PARSER SPÉCIFIQUE S4E POWER API
# ------------------------------------------------------------------
def parse_and_format_s4e(diagnostics_data, output_path):
    records = diagnostics_data.get("records", [])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Dictionnaires pour stocker les mesures par numéro de série
    mppt_devices = {}
    battery_device = {}
    inverter_device = {}

    current_mppt_serial = "mppt_default"

    for rec in records:
        code = str(rec.get("code", ""))
        val = rec.get("formattedValue", rec.get("rawValue", ""))
        
        # --------------------------------------------------------------
        # 1. MPPT / REGULATEURS SOLAIRES
        # --------------------------------------------------------------
        if code == "ScSN": # Détection du numéro de série du MPPT
            current_mppt_serial = str(val)
            if current_mppt_serial not in mppt_devices:
                mppt_devices[current_mppt_serial] = {}
        
        if current_mppt_serial in mppt_devices:
            if code == "PVP": # Puissance PV (W)
                mppt_devices[current_mppt_serial]["power"] = parse_number(val)
            elif code == "PVV": # Tension PV (V)
                mppt_devices[current_mppt_serial]["volt"] = parse_number(val)
            elif code == "ScI": # Courant de charge (A)
                mppt_devices[current_mppt_serial]["current"] = parse_number(val)
            elif code == "YT": # Énergie produite aujourd'hui (kWh)
                mppt_devices[current_mppt_serial]["energy"] = parse_number(val)

        # --------------------------------------------------------------
        # 2. BATTERIE
        # --------------------------------------------------------------
        if code == "SOC":
            battery_device["state_of_charge"] = parse_number(val)
        elif code == "V" and "volt" not in battery_device:
            battery_device["volt"] = parse_number(val)
        elif code == "I" and "current" not in battery_device:
            battery_device["current"] = parse_number(val)
        elif code == "BT": # Température Batterie (°C)
            battery_device["temperature"] = parse_number(val)
        elif code == "ca": # Capacité (Ah/kWh)
            battery_device["capacity"] = parse_number(val)
        elif code == "BP": # Puissance Batterie (W)
            battery_device["power"] = parse_number(val)

        # --------------------------------------------------------------
        # 3. ONDULEUR / CONVERTISSEUR (VE.Bus / MultiPlus)
        # --------------------------------------------------------------
        if code == "OP1": # Puissance de sortie AC (W)
            inverter_device["power"] = parse_number(val)
        elif code == "OV1": # Tension de sortie AC (V)
            inverter_device["volt"] = parse_number(val)
        elif code == "OI1": # Courant de sortie AC (A)
            inverter_device["current"] = parse_number(val)
        elif code == "IP1": # Puissance d'entrée AC / Grille (W)
            inverter_device["power_in"] = parse_number(val)
        elif code == "IV1": # Tension d'entrée AC / Grille (V)
            inverter_device["volt_in"] = parse_number(val)
        elif code == "II1": # Courant d'entrée AC / Grille (A)
            inverter_device["current_in"] = parse_number(val)
        elif code == "t9": # Énergie totale produite (kWh)
            inverter_device["energy_tot"] = parse_number(val)

    # --------------------------------------------------------------
    # CONSTRUCTION DU FICHIER CSV (Format CSV Simple S4E)
    # --------------------------------------------------------------
    headers = [
        "date", "device", "serial", 
        "power", "volt", "current", "energy", "energy_tot",
        "power_in", "volt_in", "current_in", 
        "state_of_charge", "temperature", "capacity"
    ]

    rows = []

    # Ajout des lignes MPPT
    for serial, metrics in mppt_devices.items():
        rows.append({
            "date": now_str,
            "device": "mppt",
            "serial": serial,
            "power": metrics.get("power", ""),
            "volt": metrics.get("volt", ""),
            "current": metrics.get("current", ""),
            "energy": metrics.get("energy", ""),
            "energy_tot": "", "power_in": "", "volt_in": "", "current_in": "",
            "state_of_charge": "", "temperature": "", "capacity": ""
        })

    # Ajout de la ligne Convertisseur / Onduleur
    if inverter_device:
        rows.append({
            "date": now_str,
            "device": "converter",
            "serial": f"multi_{VRM_SITE_ID}",
            "power": inverter_device.get("power", ""),
            "volt": inverter_device.get("volt", ""),
            "current": inverter_device.get("current", ""),
            "energy": "",
            "energy_tot": inverter_device.get("energy_tot", ""),
            "power_in": inverter_device.get("power_in", ""),
            "volt_in": inverter_device.get("volt_in", ""),
            "current_in": inverter_device.get("current_in", ""),
            "state_of_charge": "", "temperature": "", "capacity": ""
        })

    # Ajout de la ligne Batterie
    if battery_device:
        rows.append({
            "date": now_str,
            "device": "battery",
            "serial": f"batt_{VRM_SITE_ID}",
            "power": battery_device.get("power", ""),
            "volt": battery_device.get("volt", ""),
            "current": battery_device.get("current", ""),
            "energy": "", "energy_tot": "", "power_in": "", "volt_in": "", "current_in": "",
            "state_of_charge": battery_device.get("state_of_charge", ""),
            "temperature": battery_device.get("temperature", ""),
            "capacity": battery_device.get("capacity", "")
        })

    # Écriture du fichier CSV avec séparateur point-virgule et encodage UTF-8
    with open(output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f" CSV Power API généré avec succès : {len(rows)} équipements exportés.")

# ------------------------------------------------------------------
# ENVOI SFTP
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
    print(" Envoi SFTP réussi !")

# ------------------------------------------------------------------
# EXECUTION
# ------------------------------------------------------------------
if __name__ == "__main__":
    try:
        print("1. Lecture de l'API Victron...")
        data = fetch_victron_diagnostics()
        print("2. Formatage S4E Power API...")
        parse_and_format_s4e(data, LOCAL_FILE_PATH)
        print("3. Envoi SFTP...")
        upload_via_sftp(LOCAL_FILE_PATH, SFTP_REMOTE_PATH)
        print(" Opération terminée avec succès.")
    except Exception as e:
        print(f" Erreur : {e}")
        exit(1)
