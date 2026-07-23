import os
import csv
import requests
import paramiko
from datetime import datetime, timedelta
import zoneinfo

# ------------------------------------------------------------------
# CONFIGURATION & VARIABLES D'ENVIRONNEMENT
# ------------------------------------------------------------------
VRM_API_TOKEN = os.getenv("VRM_API_TOKEN")
VRM_SITE_ID = os.getenv("VRM_SITE_ID")

SFTP_HOST = os.getenv("SFTP_HOST")
SFTP_PORT = int(os.getenv("SFTP_PORT", 22))
SFTP_USER = os.getenv("SFTP_USER")
SFTP_PASS = os.getenv("SFTP_PASS")
# Répertoire distant SFTP (ex: "/upload/" ou "./")
SFTP_REMOTE_DIR = os.getenv("SFTP_REMOTE_PATH", "./")

# ------------------------------------------------------------------
# 1. GESTION DU HORODATAGE ET DES INTERVALLES TEMPORELS
# ------------------------------------------------------------------
TZ_FRANCE = zoneinfo.ZoneInfo("Europe/Paris")

def get_time_window_current_hour():
    """
    Calcule le début et la fin de l'heure en cours (ou heure écoulée)
    Retourne les timestamps Unix exigés par l'API Victron stats
    """
    now = datetime.now(TZ_FRANCE)
    
    # Début de l'heure en cours (ex: 12:00:00)
    start_time = now.replace(minute=0, second=0, microsecond=0)
    # Fin de l'heure en cours (ex: 12:59:59)
    end_time = now

    start_ts = int(start_time.timestamp())
    end_ts = int(end_time.timestamp())
    
    return start_ts, end_ts

def generate_dynamic_filename():
    """Génère un nom de fichier horodaté YYYYMMDD_HHMMSS.csv"""
    now_str = datetime.now(TZ_FRANCE).strftime("%Y%m%d_%H%M%S")
    return f"solar_data_{now_str}.csv"

# ------------------------------------------------------------------
# 2. RÉCUPÉRATION DES DONNÉES HISTORIQUES DE L'HEURE (API STATS)
# ------------------------------------------------------------------
def fetch_victron_hourly_stats(start_ts, end_ts):
    """
    Interroge l'API Victron stats pour obtenir tous les enregistrements de l'intervalle
    """
    url = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/stats"
    headers = {
        "x-authorization": f"Token {VRM_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Type de données à demander : kwh (production), bs (SOC batterie), etc.
    params = {
        "start": start_ts,
        "end": end_ts,
        "interval": "5mins", # Fréquence des données : 5 mins, 15 mins ou 1 hour
        "type": "custom"
    }
    
    response = requests.get(url, headers=headers, params=params, timeout=20)
    response.raise_for_status()
    return response.json()

# ------------------------------------------------------------------
# 3. CONVERSION ET EXPANSION EN FORMAT POWER API S4E
# ------------------------------------------------------------------
def parse_number(val):
    if val is None:
        return ""
    try:
        return float(val)
    except (ValueError, TypeError):
        return ""

def generate_hourly_csv(stats_data, diagnostics_data, output_file):
    """
    Associe les points d'historique issus de /stats et /diagnostics
    pour générer toutes les lignes de l'heure au format S4E Power API
    """
    records = diagnostics_data.get("records", [])
    now_tz = datetime.now(TZ_FRANCE)
    
    # 1. Identification préalable des appareils
    instances = {}
    for rec in records:
        inst_id = str(rec.get("instance", rec.get("idSite", VRM_SITE_ID)))
        code = str(rec.get("code", ""))
        val = rec.get("formattedValue", rec.get("rawValue", ""))

        if inst_id not in instances:
            instances[inst_id] = {"type": None, "serial": f"dev_{inst_id}", "metrics": {}}

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

    # 2. Extrait des dernières métriques mesurées
    for rec in records:
        inst_id = str(rec.get("instance", rec.get("idSite", VRM_SITE_ID)))
        code = str(rec.get("code", ""))
        val = rec.get("formattedValue", rec.get("rawValue", ""))
        num_val = parse_number(str(val).split()[0].replace(',', '.') if val else "")

        if inst_id not in instances or instances[inst_id]["type"] is None:
            continue

        inst = instances[inst_id]
        dev_type = inst["type"]

        if dev_type == "mppt":
            if code in ["PVP", "ScW"]: inst["metrics"]["power"] = num_val
            elif code in ["PVV", "ScV"]: inst["metrics"]["volt"] = num_val
            elif code in ["ScI"]: inst["metrics"]["current"] = num_val
            elif code in ["YT"]: inst["metrics"]["energy"] = num_val
        elif dev_type == "battery":
            if code in ["SOC", "bs"]: inst["metrics"]["state_of_charge"] = num_val
            elif code in ["V", "bv"]: inst["metrics"]["volt"] = num_val
            elif code in ["I", "bc"]: inst["metrics"]["current"] = num_val
            elif code in ["BT", "bT", "CT"]: inst["metrics"]["temperature"] = num_val
            elif code in ["ca"]: inst["metrics"]["capacity"] = num_val
            elif code in ["BP", "bp"]: inst["metrics"]["power"] = num_val
        elif dev_type == "converter":
            if code in ["OP1"]: inst["metrics"]["power"] = num_val
            elif code in ["OV1"]: inst["metrics"]["volt"] = num_val
            elif code in ["OI1"]: inst["metrics"]["current"] = num_val
            elif code in ["IP1"]: inst["metrics"]["power_in"] = num_val
            elif code in ["IV1"]: inst["metrics"]["volt_in"] = num_val
            elif code in ["II1"]: inst["metrics"]["current_in"] = num_val
            elif code in ["t9"]: inst["metrics"]["energy_tot"] = num_val

    # 3. Construction des lignes du fichier CSV
    headers = [
        "date", "device", "serial", 
        "power", "volt", "current", "energy", "energy_tot",
        "power_in", "volt_in", "current_in", 
        "state_of_charge", "temperature", "capacity"
    ]

    date_formatted = now_tz.strftime("%Y-%m-%d %H:%M:%S")
    rows = []

    for inst_id, item in instances.items():
        dev_type = item["type"]
        if dev_type not in ["mppt", "battery", "converter"]:
            continue

        metrics = item["metrics"]
        if not metrics:
            continue

        rows.append({
            "date": date_formatted,
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

    with open(output_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f" Fichier '{output_file}' généré avec succès ({len(rows)} équipements).")

# ------------------------------------------------------------------
# 4. ENVOI PAR SFTP AVEC NOM DYNAMIQUE
# ------------------------------------------------------------------
def upload_via_sftp(local_file, remote_dir):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, password=SFTP_PASS, timeout=10)
    
    sftp = ssh.open_sftp()
    
    # Nettoyage et construction du chemin distant complet
    clean_dir = remote_dir.rstrip('/') if remote_dir != './' else '.'
    remote_file_path = f"{clean_dir}/{os.path.basename(local_file)}" if clean_dir != '.' else os.path.basename(local_file)
    
    print(f" Dépôt du fichier sous : '{remote_file_path}'")
    sftp.put(local_file, remote_file_path)
    
    sftp.close()
    ssh.close()
    print(" Transfert SFTP terminé avec succès !")

# ------------------------------------------------------------------
# DÉCLENCHEMENT DU PIPELINE
# ------------------------------------------------------------------
if __name__ == "__main__":
    try:
        # A. Génération du nom de fichier dynamique
        local_filename = generate_dynamic_filename()
        
        # B. Récupération de la plage temporelle
        start_ts, end_ts = get_time_window_current_hour()
        print(f"1. Récupération des données Victron (Plage Unix : {start_ts} -> {end_ts})...")
        
        # C. Appels API
        stats_data = fetch_victron_hourly_stats(start_ts, end_ts)
        
        # Endpoint diagnostics pour la cartographie des composants
        url_diag = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/diagnostics"
        headers = {"x-authorization": f"Token {VRM_API_TOKEN}", "Content-Type": "application/json"}
        diag_data = requests.get(url_diag, headers=headers, timeout=20).json()

        # D. Génération du CSV horodaté
        print(f"2. Écriture du fichier '{local_filename}'...")
        generate_hourly_csv(stats_data, diag_data, local_filename)

        # E. Transfert SFTP
        print("3. Envoi SFTP...")
        upload_via_sftp(local_filename, SFTP_REMOTE_DIR)

        # Nettoyage du fichier local
        if os.path.exists(local_filename):
            os.remove(local_filename)
            
    except Exception as e:
        print(f" Erreur : {e}")
        exit(1)
