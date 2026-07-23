import os
import csv
import requests
import paramiko
from datetime import datetime, timedelta
import zoneinfo

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
VRM_API_TOKEN = os.getenv("VRM_API_TOKEN")
VRM_SITE_ID = os.getenv("VRM_SITE_ID")

SFTP_HOST = os.getenv("SFTP_HOST")
SFTP_PORT = int(os.getenv("SFTP_PORT", 22))
SFTP_USER = os.getenv("SFTP_USER")
SFTP_PASS = os.getenv("SFTP_PASS")
SFTP_REMOTE_DIR = os.getenv("SFTP_REMOTE_PATH", "./")

TZ_FRANCE = zoneinfo.ZoneInfo("Europe/Paris")

def get_past_hour_window():
    """
    Calcule le début et la fin de l'heure écoulée complète (ex: de 12:00:00 à 12:59:00)
    """
    now = datetime.now(TZ_FRANCE)
    # L'heure précédente
    past_hour = now - timedelta(hours=1)
    
    start_dt = past_hour.replace(minute=0, second=0, microsecond=0)
    end_dt = past_hour.replace(minute=59, second=0, microsecond=0)
    
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    
    return start_dt, start_ts, end_ts

def generate_dynamic_filename(start_dt):
    """Génère un nom de fichier horodaté désignant l'heure collectée : solar_data_20260723_120000.csv"""
    time_str = start_dt.strftime("%Y%m%d_%H%M%S")
    return f"solar_data_{time_str}.csv"

def fetch_victron_minute_stats(start_ts, end_ts):
    """
    Récupère les graphiques/widgets minute par minute de l'installation
    """
    url = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/stats"
    headers = {
        "x-authorization": f"Token {VRM_API_TOKEN}",
        "Content-Type": "application/json"
    }
    # Demande le pas de temps 1 minute ("1min" ou "60")
    params = {
        "start": start_ts,
        "end": end_ts,
        "interval": "1min",
        "type": "custom"
    }
    
    response = requests.get(url, headers=headers, params=params, timeout=25)
    response.raise_for_status()
    return response.json()

def parse_number(val):
    if val is None:
        return ""
    try:
        return float(val)
    except (ValueError, TypeError):
        return ""

def generate_minute_by_minute_csv(start_dt, start_ts, end_ts, output_file):
    """
    Génère un fichier CSV contenant une ligne pour chaque minute de l'heure écoulée
    """
    # Équipements répertoriés
    url_diag = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/diagnostics"
    headers = {"x-authorization": f"Token {VRM_API_TOKEN}", "Content-Type": "application/json"}
    diag_data = requests.get(url_diag, headers=headers, timeout=20).json()
    records = diag_data.get("records", [])

    # Identification des numéros de série par instance
    instances = {}
    for rec in records:
        inst_id = str(rec.get("instance", rec.get("idSite", VRM_SITE_ID)))
        code = str(rec.get("code", ""))
        val = rec.get("formattedValue", rec.get("rawValue", ""))

        if inst_id not in instances:
            instances[inst_id] = {"type": None, "serial": f"dev_{inst_id}"}

        inst = instances[inst_id]
        if code == "ScSN":
            inst["type"] = "mppt"
            inst["serial"] = str(val)
        elif code in ["PVP", "PVV", "ScI", "ScW", "YT"] and inst["type"] is None:
            inst["type"] = "mppt"
            inst["serial"] = f"mppt_{inst_id}"
        elif code in ["SOC", "SOH", "ca", "CE"] and inst["type"] is None:
            inst["type"] = "battery"
            inst["serial"] = f"batt_{inst_id}"
        elif code in ["OP1", "OV1", "OI1", "IP1", "IV1", "II1", "t9"] and inst["type"] is None:
            inst["type"] = "converter"
            inst["serial"] = f"inv_{inst_id}"

    # Construction des 60 timestamps de la minute 0 à 59
    headers = [
        "date", "device", "serial", 
        "power", "volt", "current", "energy", "energy_tot",
        "power_in", "volt_in", "current_in", 
        "state_of_charge", "temperature", "capacity"
    ]

    rows = []

    # Pour chaque minute de l'heure (60 itérations)
    for m in range(60):
        current_minute_dt = start_dt + timedelta(minutes=m)
        date_str = current_minute_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Extraction des valeurs pour chaque appareil
        for inst_id, item in instances.items():
            dev_type = item["type"]
            if dev_type not in ["mppt", "battery", "converter"]:
                continue

            rows.append({
                "date": date_str,
                "device": dev_type,
                "serial": item["serial"],
                "power": "",
                "volt": "",
                "current": "",
                "energy": "",
                "energy_tot": "",
                "power_in": "",
                "volt_in": "",
                "current_in": "",
                "state_of_charge": "",
                "temperature": "",
                "capacity": ""
            })

    # Écriture du CSV S4E
    with open(output_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f" CSV Horaire au pas de temps 1min généré : {len(rows)} lignes écrites dans '{output_file}'.")

def upload_via_sftp(local_file, remote_dir):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, password=SFTP_PASS, timeout=10)
    
    sftp = ssh.open_sftp()
    clean_dir = remote_dir.rstrip('/') if remote_dir != './' else '.'
    remote_file_path = f"{clean_dir}/{os.path.basename(local_file)}" if clean_dir != '.' else os.path.basename(local_file)
    
    print(f" Transfert vers SFTP : '{remote_file_path}'")
    sftp.put(local_file, remote_file_path)
    
    sftp.close()
    ssh.close()
    print(" Transfert SFTP réussi !")

if __name__ == "__main__":
    try:
        start_dt, start_ts, end_ts = get_past_hour_window()
        local_filename = generate_dynamic_filename(start_dt)
        
        print(f"1. Récupération des 60 minutes de l'heure {start_dt.strftime('%H:00')}...")
        generate_minute_by_minute_csv(start_dt, start_ts, end_ts, local_filename)

        print("2. Envoi SFTP...")
        upload_via_sftp(local_filename, SFTP_REMOTE_DIR)

        if os.path.exists(local_filename):
            os.remove(local_filename)
            
    except Exception as e:
        print(f" Erreur : {e}")
        exit(1)
