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
    Calcule le début et la fin de l'heure écoulée (ex: de 11:00:00 à 11:59:59)
    """
    now = datetime.now(TZ_FRANCE)
    past_hour = now - timedelta(hours=1)
    
    start_dt = past_hour.replace(minute=0, second=0, microsecond=0)
    end_dt = past_hour.replace(minute=59, second=59, microsecond=0)
    
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    
    return start_dt, start_ts, end_ts

def generate_dynamic_filename(start_dt):
    """Génère un nom de fichier horodaté YYYYMMDD_HHMMSS.csv"""
    time_str = start_dt.strftime("%Y%m%d_%H%M%S")
    return f"solar_data_{time_str}.csv"

def parse_number(val):
    if val is None:
        return ""
    try:
        return float(val)
    except (ValueError, TypeError):
        return ""

def fetch_and_build_csv(start_dt, start_ts, end_ts, output_file):
    headers_api = {
        "x-authorization": f"Token {VRM_API_TOKEN}",
        "Content-Type": "application/json"
    }

    # 1. Cartographie des équipements via /diagnostics
    url_diag = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/diagnostics"
    diag_res = requests.get(url_diag, headers=headers_api, timeout=20).json()
    records_diag = diag_res.get("records", [])

    instances = {}
    for rec in records_diag:
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

    # 2. Récupération des données minute par minute via /data-download
    url_data = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/data-download"
    params = {
        "start": start_ts,
        "end": end_ts,
        "datatype": "kwh"
    }
    
    # On récupère aussi les données instantanées de l'heure écoulée
    url_stats = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/stats"
    params_stats = {
        "start": start_ts,
        "end": end_ts,
        "interval": "1mins",
        "type": "custom"
    }
    
    stats_res = requests.get(url_stats, headers=headers_api, params=params_stats, timeout=25).json()
    
    # Extraction des points de mesure minute par minute
    # Dictionnaire : (minute_index, instance_id) -> {metrics}
    time_series = {}

    records_stats = stats_res.get("records", {})
    
    # Parcourt les données minute de l'API VRM
    if isinstance(records_stats, dict):
        for code, item in records_stats.items():
            if isinstance(item, dict) and "data" in item:
                data_points = item.get("data", [])
                for point in data_points:
                    if isinstance(point, list) and len(point) >= 2:
                        ts = point[0] / 1000.0  # Timestamp Unix en s
                        val = point[1]
                        dt_pt = datetime.fromtimestamp(ts, tz=TZ_FRANCE)
                        dt_key = dt_pt.strftime("%Y-%m-%d %H:%M:00")
                        
                        if dt_key not in time_series:
                            time_series[dt_key] = {}
                        
                        # Attribution du code de registre au timestamp
                        time_series[dt_key][code] = parse_number(val)

    # 3. Écriture des 60 minutes au format CSV S4E Power API
    headers_csv = [
        "date", "device", "serial", 
        "power", "volt", "current", "energy", "energy_tot",
        "power_in", "volt_in", "current_in", 
        "state_of_charge", "temperature", "capacity"
    ]

    rows = []
    
    # Génération des 60 minutes exactes de l'heure (de m=0 à m=59)
    for m in range(60):
        minute_dt = start_dt + timedelta(minutes=m)
        minute_str = minute_dt.strftime("%Y-%m-%d %H:%M:00")
        metrics_pt = time_series.get(minute_str, {})

        for inst_id, item in instances.items():
            dev_type = item["type"]
            if dev_type not in ["mppt", "battery", "converter"]:
                continue

            # Extraction des valeurs si présentes à cette minute
            pwr = metrics_pt.get("PVP", metrics_pt.get("OP1", metrics_pt.get("P", "")))
            vlt = metrics_pt.get("PVV", metrics_pt.get("OV1", metrics_pt.get("V", "")))
            cur = metrics_pt.get("ScI", metrics_pt.get("OI1", metrics_pt.get("I", "")))
            soc = metrics_pt.get("SOC", metrics_pt.get("bs", ""))
            temp = metrics_pt.get("BT", metrics_pt.get("CT", ""))

            rows.append({
                "date": minute_str,
                "device": dev_type,
                "serial": item["serial"],
                "power": pwr,
                "volt": vlt,
                "current": cur,
                "energy": metrics_pt.get("YT", ""),
                "energy_tot": metrics_pt.get("t9", ""),
                "power_in": metrics_pt.get("IP1", ""),
                "volt_in": metrics_pt.get("IV1", ""),
                "current_in": metrics_pt.get("II1", ""),
                "state_of_charge": soc,
                "temperature": temp,
                "capacity": metrics_pt.get("ca", "")
            })

    with open(output_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers_csv, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f" Génération terminée : {len(rows)} lignes écrites dans '{output_file}'.")

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
        
        print(f"1. Récupération des données minute par minute pour l'heure {start_dt.strftime('%H:00')}...")
        fetch_and_build_csv(start_dt, start_ts, end_ts, local_filename)

        print("2. Envoi SFTP...")
        upload_via_sftp(local_filename, SFTP_REMOTE_DIR)

        if os.path.exists(local_filename):
            os.remove(local_filename)
            
    except Exception as e:
        print(f" Erreur : {e}")
        exit(1)
