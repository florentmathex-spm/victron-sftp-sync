import os
import csv
import requests
import paramiko
from datetime import datetime, timedelta
import zoneinfo

# ------------------------------------------------------------------
# CONFIGURATION & SECRETS
# ------------------------------------------------------------------
VRM_API_TOKEN = os.getenv("VRM_API_TOKEN")
VRM_SITE_ID = os.getenv("VRM_SITE_ID")

SFTP_HOST = os.getenv("SFTP_HOST")
SFTP_PORT = int(os.getenv("SFTP_PORT", 22))
SFTP_USER = os.getenv("SFTP_USER")
SFTP_PASS = os.getenv("SFTP_PASS")
SFTP_REMOTE_DIR = os.getenv("SFTP_REMOTE_PATH", "./")

TZ_FRANCE = zoneinfo.ZoneInfo("Europe/Paris")

# ------------------------------------------------------------------
# GESTION DU HORODATAGE ET PAS DE TEMPS 10 MIN
# ------------------------------------------------------------------
def get_past_hour_window():
    """
    Calcule la fenêtre de l'heure écoulee (ex: de 12:00:00 à 12:59:59)
    """
    now = datetime.now(TZ_FRANCE)
    past_hour = now - timedelta(hours=1)
    
    start_dt = past_hour.replace(minute=0, second=0, microsecond=0)
    end_dt = past_hour.replace(minute=59, second=59, microsecond=0)
    
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    
    return start_dt, start_ts, end_ts

def generate_dynamic_filename(start_dt):
    time_str = start_dt.strftime("%Y%m%d_%H%M%S")
    return f"solar_data_{time_str}.csv"

def parse_number(val):
    if val is None:
        return ""
    try:
        s = str(val).strip().split()[0].replace(',', '.')
        return float(s)
    except (ValueError, TypeError, IndexError):
        return ""

# ------------------------------------------------------------------
# EXTRACTION ET FORMATAGE DES DONNÉES S4E POWER API (PAS 10 MIN)
# ------------------------------------------------------------------
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
    last_known = {}

    for rec in records_diag:
        inst_id = str(rec.get("instance", rec.get("idSite", VRM_SITE_ID)))
        code = str(rec.get("code", ""))
        val = rec.get("formattedValue", rec.get("rawValue", ""))
        num_val = parse_number(val)

        if inst_id not in instances:
            instances[inst_id] = {"type": None, "serial": f"dev_{inst_id}"}
            last_known[inst_id] = {}

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

        # Conservation des valeurs connues pour fallback
        dev_type = inst["type"]
        if dev_type == "mppt":
            if code in ["PVP", "ScW"]: last_known[inst_id]["power"] = num_val
            elif code in ["PVV", "ScV"]: last_known[inst_id]["volt"] = num_val
            elif code in ["ScI"]: last_known[inst_id]["current"] = num_val
            elif code in ["YT"]: last_known[inst_id]["energy"] = num_val
        elif dev_type == "battery":
            if code in ["SOC", "bs"]: last_known[inst_id]["state_of_charge"] = num_val
            elif code in ["V", "bv"]: last_known[inst_id]["volt"] = num_val
            elif code in ["I", "bc"]: last_known[inst_id]["current"] = num_val
            elif code in ["BT", "bT", "CT"]: last_known[inst_id]["temperature"] = num_val
            elif code in ["ca"]: last_known[inst_id]["capacity"] = num_val
            elif code in ["BP", "bp"]: last_known[inst_id]["power"] = num_val
        elif dev_type == "converter":
            if code in ["OP1"]: last_known[inst_id]["power"] = num_val
            elif code in ["OV1"]: last_known[inst_id]["volt"] = num_val
            elif code in ["OI1"]: last_known[inst_id]["current"] = num_val
            elif code in ["IP1"]: last_known[inst_id]["power_in"] = num_val
            elif code in ["IV1"]: last_known[inst_id]["volt_in"] = num_val
            elif code in ["II1"]: last_known[inst_id]["current_in"] = num_val
            elif code in ["t9"]: last_known[inst_id]["energy_tot"] = num_val

    # 2. Récupération des séries temporelles
    url_graph = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/widgets/Graph"
    params_graph = {
        "start": start_ts,
        "end": end_ts,
        "interval": "10mins"
    }
    
    time_series = {}
    try:
        graph_res = requests.get(url_graph, headers=headers_api, params=params_graph, timeout=25).json()
        records_graph = graph_res.get("records", {})
        data_records = records_graph.get("data", {}) if isinstance(records_graph, dict) else {}

        if isinstance(data_records, dict):
            for attr_code, meta in data_records.items():
                if isinstance(meta, dict) and "values" in meta:
                    points = meta.get("values", [])
                    for pt in points:
                        if isinstance(pt, list) and len(pt) >= 2:
                            ts = pt[0] / 1000.0
                            val = pt[1]
                            dt_pt = datetime.fromtimestamp(ts, tz=TZ_FRANCE)
                            dt_key = dt_pt.strftime("%Y-%m-%d %H:%M:00")

                            if dt_key not in time_series:
                                time_series[dt_key] = {}
                            time_series[dt_key][attr_code] = parse_number(val)
    except Exception as e:
        print(f" Note API Graph : {e}")

    # 3. Écriture du CSV (pas de temps 10 min : 0, 10, 20, 30, 40, 50 min)
    headers_csv = [
        "date", "device", "serial", 
        "power", "volt", "current", "energy", "energy_tot",
        "power_in", "volt_in", "current_in", 
        "state_of_charge", "temperature", "capacity"
    ]

    rows = []
    for m in range(0, 60, 10):
        step_dt = start_dt + timedelta(minutes=m)
        step_str = step_dt.strftime("%Y-%m-%d %H:%M:00")
        metrics_pt = time_series.get(step_str, {})

        for inst_id, item in instances.items():
            dev_type = item["type"]
            if dev_type not in ["mppt", "battery", "converter"]:
                continue

            known = last_known.get(inst_id, {})

            pwr = metrics_pt.get("PVP", metrics_pt.get("OP1", metrics_pt.get("P", known.get("power", ""))))
            vlt = metrics_pt.get("PVV", metrics_pt.get("OV1", metrics_pt.get("V", known.get("volt", ""))))
            cur = metrics_pt.get("ScI", metrics_pt.get("OI1", metrics_pt.get("I", known.get("current", ""))))
            soc = metrics_pt.get("SOC", metrics_pt.get("bs", known.get("state_of_charge", "")))
            temp = metrics_pt.get("BT", metrics_pt.get("CT", known.get("temperature", "")))
            
            p_in = metrics_pt.get("IP1", known.get("power_in", ""))
            v_in = metrics_pt.get("IV1", known.get("volt_in", ""))
            c_in = metrics_pt.get("II1", known.get("current_in", ""))
            e_tot = metrics_pt.get("t9", known.get("energy_tot", ""))
            eng = metrics_pt.get("YT", known.get("energy", ""))
            cap = known.get("capacity", "")

            if pwr != "": known["power"] = pwr
            if vlt != "": known["volt"] = vlt
            if cur != "": known["current"] = cur
            if soc != "": known["state_of_charge"] = soc
            if temp != "": known["temperature"] = temp

            rows.append({
                "date": step_str,
                "device": dev_type,
                "serial": item["serial"],
                "power": pwr,
                "volt": vlt,
                "current": cur,
                "energy": eng,
                "energy_tot": e_tot,
                "power_in": p_in,
                "volt_in": v_in,
                "current_in": c_in,
                "state_of_charge": soc,
                "temperature": temp,
                "capacity": cap
            })

    abs_output_path = os.path.abspath(output_file)
    with open(abs_output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers_csv, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f" Génération CSV réussie ({len(rows)} lignes générées).")
    return abs_output_path

# ------------------------------------------------------------------
# ENVOI SFTP ROBUSTE (AVEC GESTION DU REPERTOIRE ET DES DROITS)
# ------------------------------------------------------------------
def upload_via_sftp(local_abs_path, remote_dir_config):
    if not os.path.exists(local_abs_path):
        raise FileNotFoundError(f"Le fichier local '{local_abs_path}' est introuvable.")

    filename = os.path.basename(local_abs_path)
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=SFTP_HOST,
        port=SFTP_PORT,
        username=SFTP_USER,
        password=SFTP_PASS,
        look_for_keys=False,
        allow_agent=False,
        timeout=15
    )
    
    sftp = ssh.open_sftp()

    # Nettoyage sécurisé du dossier distant pour éviter l'écriture sur la racine système /
    clean_dir = (remote_dir_config or "").strip()
    
    if clean_dir in ["", ".", "./", "/"]:
        remote_target = filename
    else:
        # Supprime le slash initial qui provoque l'Errno 13
        clean_dir = clean_dir.lstrip('/')
        remote_target = f"{clean_dir}/{filename}" if not clean_dir.endswith('/') else f"{clean_dir}{filename}"

    print(f" Transfert du fichier vers le serveur SFTP (Destination: '{remote_target}')...")
    
    try:
        sftp.put(local_abs_path, remote_target)
        print(" Transfert SFTP réussi avec succès !")
    except PermissionError:
        print(f" ERREUR DROITS : Permission refusée sur '{remote_target}'. Tentative de secours dans le dossier distant par défaut...")
        sftp.put(local_abs_path, filename)
        print(" Transfert de secours réussi !")
    finally:
        sftp.close()
        ssh.close()

# ------------------------------------------------------------------
# DÉCLENCHEMENT
# ------------------------------------------------------------------
if __name__ == "__main__":
    try:
        start_dt, start_ts, end_ts = get_past_hour_window()
        filename = generate_dynamic_filename(start_dt)
        
        print(f"1. Récupération des données au pas de temps 10 min pour l'heure {start_dt.strftime('%H:00')}...")
        abs_file_path = fetch_and_build_csv(start_dt, start_ts, end_ts, filename)

        print("2. Envoi SFTP...")
        upload_via_sftp(abs_file_path, SFTP_REMOTE_DIR)

        if os.path.exists(abs_file_path):
            os.remove(abs_file_path)
            
    except Exception as e:
        print(f" Erreur : {e}")
        exit(1)
