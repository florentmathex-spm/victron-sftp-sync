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
# UTILITAIRES
# ------------------------------------------------------------------
def get_past_hour_window():
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

def calc_current(power, volt):
    """Calcule le courant I = P / V si la tension est strictement positive"""
    if isinstance(power, (int, float)) and isinstance(volt, (int, float)) and volt > 0:
        return round(power / volt, 2)
    return 0.0

# ------------------------------------------------------------------
# EXTRACTION & CONSTRUCTION DU CSV VIRTUEL (CUSTOM1 & BATTERIE1)
# ------------------------------------------------------------------
def fetch_and_build_csv(start_dt, start_ts, end_ts, output_file):
    headers_api = {
        "x-authorization": f"Token {VRM_API_TOKEN}",
        "Content-Type": "application/json"
    }

    # 1. Cartographie et dernières valeurs instantanées via /diagnostics
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
        elif code in ["PVP", "PVV", "ScI", "ScW", "YT", "PVV0", "PVP0"] and inst["type"] is None:
            inst["type"] = "mppt"
            inst["serial"] = f"mppt_{inst_id}"
        elif code in ["SOC", "SOH", "ca", "CE"] and inst["type"] is None:
            inst["type"] = "battery"
            inst["serial"] = f"batt_{inst_id}"
        elif code in ["OP1", "OV1", "OI1", "IP1", "IV1", "II1", "t9"] and inst["type"] is None:
            inst["type"] = "converter"
            inst["serial"] = f"inv_{inst_id}"

        dev_type = inst["type"]
        if dev_type == "mppt":
            if code == "PVV0": last_known[inst_id]["volt_t1"] = num_val
            elif code == "PVP0": last_known[inst_id]["power_t1"] = num_val
            elif code == "PVV1": last_known[inst_id]["volt_t2"] = num_val
            elif code == "PVP1": last_known[inst_id]["power_t2"] = num_val
            elif code in ["PVP", "ScW"]: last_known[inst_id]["power"] = num_val
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

    # 2. Récupération des séries temporelles (10min)
    url_graph = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/widgets/Graph"
    params_graph = {"start": start_ts, "end": end_ts, "interval": "10mins"}
    
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
        print(f" Note Graph API : {e}")

    # 3. Structure exacte du CSV S4E Power API
    headers_csv = [
        "date", "device", "serial",
        "current.mppt.1", "power.mppt.1", "volt.mppt.1",
        "current.mppt.2", "power.mppt.2", "volt.mppt.2",
        "current.mppt.3", "power.mppt.3", "volt.mppt.3",
        "current.mppt.4", "power.mppt.4", "volt.mppt.4",
        "power", "volt", "current", "energy", "energy_tot",
        "power_in", "volt_in", "current_in",
        "state_of_charge", "temperature", "capacity"
    ]

    rows = []

    # Génération des 6 points temporels (:00, :10, :20, :30, :40, :50)
    for m in range(0, 60, 10):
        step_dt = start_dt + timedelta(minutes=m)
        step_str = step_dt.strftime("%Y-%m-%d %H:%M:%S")
        metrics_pt = time_series.get(step_str[:16] + ":00", {})

        # Dictionnaires pour stocker les 4 MPPT de l'onduleur CUSTOM1
        mppt_values = {
            1: {"power": "", "volt": "", "current": ""},
            2: {"power": "", "volt": "", "current": ""},
            3: {"power": "", "volt": "", "current": ""},
            4: {"power": "", "volt": "", "current": ""}
        }

        tot_pv_power = 0.0
        has_pv_data = False

        # Variables Onduleur AC
        inv_power = ""
        inv_volt = ""
        inv_current = ""
        inv_power_in = ""
        inv_volt_in = ""
        inv_current_in = ""
        inv_energy_tot = ""

        # Variables Batterie
        batt_soc = ""
        batt_volt = ""
        batt_current = ""
        batt_power = ""
        batt_temp = ""
        batt_cap = ""

        for inst_id, item in instances.items():
            dev_type = item["type"]
            serial = item["serial"]
            known = last_known.get(inst_id, {})

            # --- Rrassemblement des 4 MPPTs pour CUSTOM1 ---
            if dev_type == "mppt":
                # MPPT 1 & 2 (Bi-tracker HQ2506CVYX4)
                if "HQ2506CVYX4" in serial or "PVV0" in metrics_pt or "volt_t1" in known:
                    v1 = metrics_pt.get("PVV0", known.get("volt_t1", ""))
                    p1 = metrics_pt.get("PVP0", known.get("power_t1", ""))
                    c1 = calc_current(p1, v1)

                    v2 = metrics_pt.get("PVV1", known.get("volt_t2", ""))
                    p2 = metrics_pt.get("PVP1", known.get("power_t2", ""))
                    c2 = calc_current(p2, v2)

                    mppt_values[1] = {"power": p1, "volt": v1, "current": c1}
                    mppt_values[2] = {"power": p2, "volt": v2, "current": c2}

                    if isinstance(p1, (int, float)): tot_pv_power += p1; has_pv_data = True
                    if isinstance(p2, (int, float)): tot_pv_power += p2; has_pv_data = True

                # MPPT 3 (HQ2441TVZCW)
                elif "HQ2441TVZCW" in serial or "278" in inst_id:
                    p3 = metrics_pt.get("PVP", metrics_pt.get("ScW", known.get("power", "")))
                    v3 = metrics_pt.get("PVV", metrics_pt.get("ScV", known.get("volt", "")))
                    c3 = metrics_pt.get("ScI", known.get("current", calc_current(p3, v3)))

                    mppt_values[3] = {"power": p3, "volt": v3, "current": c3}
                    if isinstance(p3, (int, float)): tot_pv_power += p3; has_pv_data = True

                # MPPT 4 (HQ2441W3N4G)
                else:
                    p4 = metrics_pt.get("PVP", metrics_pt.get("ScW", known.get("power", "")))
                    v4 = metrics_pt.get("PVV", metrics_pt.get("ScV", known.get("volt", "")))
                    c4 = metrics_pt.get("ScI", known.get("current", calc_current(p4, v4)))

                    mppt_values[4] = {"power": p4, "volt": v4, "current": c4}
                    if isinstance(p4, (int, float)): tot_pv_power += p4; has_pv_data = True

            # --- Extraction des données de l'onduleur / MultiPlus ---
            elif dev_type == "converter":
                inv_power = metrics_pt.get("OP1", known.get("power", ""))
                inv_volt = metrics_pt.get("OV1", known.get("volt", ""))
                inv_current = metrics_pt.get("OI1", known.get("current", ""))
                inv_power_in = metrics_pt.get("IP1", known.get("power_in", ""))
                inv_volt_in = metrics_pt.get("IV1", known.get("volt_in", ""))
                inv_current_in = metrics_pt.get("II1", known.get("current_in", ""))
                inv_energy_tot = metrics_pt.get("t9", known.get("energy_tot", ""))

            # --- Extraction des données de la batterie ---
            elif dev_type == "battery":
                batt_soc = metrics_pt.get("SOC", metrics_pt.get("bs", known.get("state_of_charge", "")))
                batt_volt = metrics_pt.get("V", metrics_pt.get("bv", known.get("volt", "")))
                batt_current = metrics_pt.get("I", metrics_pt.get("bc", known.get("current", "")))
                batt_power = metrics_pt.get("BP", metrics_pt.get("bp", known.get("power", "")))
                batt_temp = metrics_pt.get("BT", metrics_pt.get("CT", known.get("temperature", "")))
                batt_cap = known.get("capacity", "")

        # --------------------------------------------------------------
        # LIGNE 1 : ONDULEUR VIRTUEL "CUSTOM1" (4 MPPT DC + Côté AC)
        # --------------------------------------------------------------
        rows.append({
            "date": step_str,
            "device": "inverter",
            "serial": "CUSTOM1",
            "current.mppt.1": mppt_values[1]["current"],
            "power.mppt.1": mppt_values[1]["power"],
            "volt.mppt.1": mppt_values[1]["volt"],
            "current.mppt.2": mppt_values[2]["current"],
            "power.mppt.2": mppt_values[2]["power"],
            "volt.mppt.2": mppt_values[2]["volt"],
            "current.mppt.3": mppt_values[3]["current"],
            "power.mppt.3": mppt_values[3]["power"],
            "volt.mppt.3": mppt_values[3]["volt"],
            "current.mppt.4": mppt_values[4]["current"],
            "power.mppt.4": mppt_values[4]["power"],
            "volt.mppt.4": mppt_values[4]["volt"],
            "power": inv_power if inv_power != "" else (tot_pv_power if has_pv_data else ""),
            "volt": inv_volt,
            "current": inv_current,
            "energy": "",
            "energy_tot": inv_energy_tot,
            "power_in": inv_power_in,
            "volt_in": inv_volt_in,
            "current_in": inv_current_in,
            "state_of_charge": "",
            "temperature": "",
            "capacity": ""
        })

        # --------------------------------------------------------------
        # LIGNE 2 : BATTERIE VIRTUELLE "BATTERIE1"
        # --------------------------------------------------------------
        rows.append({
            "date": step_str,
            "device": "battery",
            "serial": "BATTERIE1",
            "current.mppt.1": "", "power.mppt.1": "", "volt.mppt.1": "",
            "current.mppt.2": "", "power.mppt.2": "", "volt.mppt.2": "",
            "current.mppt.3": "", "power.mppt.3": "", "volt.mppt.3": "",
            "current.mppt.4": "", "power.mppt.4": "", "volt.mppt.4": "",
            "power": batt_power,
            "volt": batt_volt,
            "current": batt_current,
            "energy": "",
            "energy_tot": "",
            "power_in": "",
            "volt_in": "",
            "current_in": "",
            "state_of_charge": batt_soc,
            "temperature": batt_temp,
            "capacity": batt_cap
        })

    abs_output_path = os.path.abspath(output_file)
    with open(abs_output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers_csv, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f" Génération réussie pour CUSTOM1 & BATTERIE1 ({len(rows)} lignes créées).")
    return abs_output_path

# ------------------------------------------------------------------
# ENVOI SFTP ROBUSTE
# ------------------------------------------------------------------
def upload_via_sftp(local_abs_path, remote_dir_config):
    if not os.path.exists(local_abs_path):
        raise FileNotFoundError(f"Le fichier local '{local_abs_path}' est introuvable.")

    filename = os.path.basename(local_abs_path)
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, password=SFTP_PASS,
        look_for_keys=False, allow_agent=False, timeout=15
    )
    
    sftp = ssh.open_sftp()
    clean_dir = (remote_dir_config or "").strip()
    
    if clean_dir in ["", ".", "./", "/"]:
        remote_target = filename
    else:
        clean_dir = clean_dir.lstrip('/')
        remote_target = f"{clean_dir}/{filename}" if not clean_dir.endswith('/') else f"{clean_dir}{filename}"

    print(f" Transfert SFTP vers : '{remote_target}'...")
    
    try:
        sftp.put(local_abs_path, remote_target)
        print(" Transfert SFTP réussi avec succès !")
    except PermissionError:
        print(f" ERREUR DROITS : Permission refusée sur '{remote_target}'. Dépôt de secours...")
        sftp.put(local_abs_path, filename)
        print(" Transfert de secours réussi !")
    finally:
        sftp.close()
        ssh.close()

# ------------------------------------------------------------------
# EXECUTION
# ------------------------------------------------------------------
if __name__ == "__main__":
    try:
        start_dt, start_ts, end_ts = get_past_hour_window()
        filename = generate_dynamic_filename(start_dt)
        
        print(f"1. Génération des séries (CUSTOM1 & BATTERIE1) pour l'heure {start_dt.strftime('%H:00')}...")
        abs_file_path = fetch_and_build_csv(start_dt, start_ts, end_ts, filename)

        print("2. Envoi SFTP...")
        upload_via_sftp(abs_file_path, SFTP_REMOTE_DIR)

        if os.path.exists(abs_file_path):
            os.remove(abs_file_path)
            
    except Exception as e:
        print(f" Erreur : {e}")
        exit(1)
