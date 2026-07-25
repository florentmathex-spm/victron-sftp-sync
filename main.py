import os
import csv
import requests
import paramiko
from datetime import datetime
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
def get_french_now():
    return datetime.now(TZ_FRANCE)

def generate_dynamic_filename(dt_now):
    time_str = dt_now.strftime("%Y%m%d_%H%M%S")
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
    """Calcule I = P / V si la tension est strictement positive"""
    if isinstance(power, (int, float)) and isinstance(volt, (int, float)) and volt > 0:
        return round(power / volt, 2)
    return 0.0

# ------------------------------------------------------------------
# EXTRACTION & CONSTRUCTION DU POINT UNIQUE (CUSTOM1 & BATTERIE1)
# ------------------------------------------------------------------
def fetch_instantaneous_and_build_csv(dt_now, output_file):
    headers_api = {
        "x-authorization": f"Token {VRM_API_TOKEN}",
        "Content-Type": "application/json"
    }

    # Interrogation directe de l'endpoint diagnostics
    url_diag = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/diagnostics"
    diag_res = requests.get(url_diag, headers=headers_api, timeout=20).json()
    records_diag = diag_res.get("records", [])

    instances = {}
    metrics_by_instance = {}

    for rec in records_diag:
        inst_id = str(rec.get("instance", rec.get("idSite", VRM_SITE_ID)))
        code = str(rec.get("code", ""))
        val = rec.get("formattedValue", rec.get("rawValue", ""))
        num_val = parse_number(val)

        if inst_id not in instances:
            instances[inst_id] = {"type": None, "serial": f"dev_{inst_id}"}
            metrics_by_instance[inst_id] = {}

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
        met = metrics_by_instance[inst_id]

        if dev_type == "mppt":
            if code == "PVV0": met["volt_t1"] = num_val
            elif code == "PVP0": met["power_t1"] = num_val
            elif code == "PVV1": met["volt_t2"] = num_val
            elif code == "PVP1": met["power_t2"] = num_val
            elif code in ["PVP", "ScW"]: met["power"] = num_val
            elif code in ["PVV", "ScV"]: met["volt"] = num_val
            elif code in ["ScI"]: met["current"] = num_val

        elif dev_type == "battery":
            if code in ["SOC", "bs"]: met["state_of_charge"] = num_val
            elif code in ["V", "bv"]: met["volt"] = num_val
            elif code in ["I", "bc"]: met["current"] = num_val
            elif code in ["BT", "bT", "CT"]: met["temperature"] = num_val
            elif code in ["ca"]: met["capacity"] = num_val
            elif code in ["BP", "bp"]: met["power"] = num_val

        elif dev_type == "converter":
            if code in ["OP1"]: met["power"] = num_val
            elif code in ["OV1"]: met["volt"] = num_val
            elif code in ["OI1"]: met["current"] = num_val
            elif code in ["IP1"]: met["power_in"] = num_val
            elif code in ["IV1"]: met["volt_in"] = num_val
            elif code in ["II1"]: met["current_in"] = num_val
            elif code in ["t9"]: met["energy_tot"] = num_val

    # Dictionnaires pour stocker les 4 MPPT de l'onduleur CUSTOM1
    mppt_values = {
        1: {"power": "", "volt": "", "current": ""},
        2: {"power": "", "volt": "", "current": ""},
        3: {"power": "", "volt": "", "current": ""},
        4: {"power": "", "volt": "", "current": ""}
    }

    tot_pv_power = 0.0
    has_pv_data = False

    inv_power = ""
    inv_volt = ""
    inv_current = ""
    inv_power_in = ""
    inv_volt_in = ""
    inv_current_in = ""
    inv_energy_tot = ""

    batt_soc = ""
    batt_volt = ""
    batt_current = ""
    batt_power = ""
    batt_temp = ""
    batt_cap = ""

    for inst_id, item in instances.items():
        dev_type = item["type"]
        serial = item["serial"]
        met = metrics_by_instance.get(inst_id, {})

        if dev_type == "mppt":
            # MPPT Bi-tracker (HQ2506CVYX4)
            if "HQ2506CVYX4" in serial or "volt_t1" in met or "power_t1" in met:
                v1 = met.get("volt_t1", "")
                p1 = met.get("power_t1", "")
                c1 = calc_current(p1, v1)

                v2 = met.get("volt_t2", "")
                p2 = met.get("power_t2", "")
                c2 = calc_current(p2, v2)

                mppt_values[1] = {"power": p1, "volt": v1, "current": c1}
                mppt_values[2] = {"power": p2, "volt": v2, "current": c2}

                if isinstance(p1, (int, float)): tot_pv_power += p1; has_pv_data = True
                if isinstance(p2, (int, float)): tot_pv_power += p2; has_pv_data = True

            # MPPT 3 (HQ2441TVZCW)
            elif "HQ2441TVZCW" in serial or "278" in inst_id:
                p3 = met.get("power", "")
                v3 = met.get("volt", "")
                c3 = met.get("current", calc_current(p3, v3))

                mppt_values[3] = {"power": p3, "volt": v3, "current": c3}
                if isinstance(p3, (int, float)): tot_pv_power += p3; has_pv_data = True

            # MPPT 4 (HQ2441W3N4G)
            else:
                p4 = met.get("power", "")
                v4 = met.get("volt", "")
                c4 = met.get("current", calc_current(p4, v4))

                mppt_values[4] = {"power": p4, "volt": v4, "current": c4}
                if isinstance(p4, (int, float)): tot_pv_power += p4; has_pv_data = True

        elif dev_type == "converter":
            inv_power = met.get("power", "")
            inv_volt = met.get("volt", "")
            inv_current = met.get("current", "")
            inv_power_in = met.get("power_in", "")
            inv_volt_in = met.get("volt_in", "")
            inv_current_in = met.get("current_in", "")
            inv_energy_tot = met.get("energy_tot", "")

        elif dev_type == "battery":
            batt_soc = met.get("state_of_charge", "")
            batt_volt = met.get("volt", "")
            batt_current = met.get("current", "")
            batt_power = met.get("power", "")
            batt_temp = met.get("temperature", "")
            batt_cap = met.get("capacity", "")

    # Structure du CSV S4E Power API
    headers_csv = [
    "date", "device", "serial",
    "current.mppt.1", "power.mppt.1", "volt.mppt.1", "energy.mppt.1",
    "current.mppt.2", "power.mppt.2", "volt.mppt.2", "energy.mppt.2",
    "current.mppt.3", "power.mppt.3", "volt.mppt.3", "energy.mppt.3",
    "current.mppt.4", "power.mppt.4", "volt.mppt.4", "energy.mppt.4",
    "power", "volt", "current", "energy", "energy_tot",
    "power_in", "volt_in", "current_in",
    "state_of_charge", "temperature", "capacity"
    ]

    date_str = dt_now.strftime("%Y-%m-%d %H:%M:%S")
    rows = []

    # Extraction des énergies depuis les données VRM (vrm_data)
    # Remplacez les clés par celles renvoyées par votre point API VRM (ex: yield_mppt1, ac_out_energy, etc.)
    
    energy_mppt1 = parse_number(vrm_data.get("yield_mppt1"))
    energy_mppt2 = parse_number(vrm_data.get("yield_mppt2"))
    energy_mppt3 = parse_number(vrm_data.get("yield_mppt3"))
    energy_mppt4 = parse_number(vrm_data.get("yield_mppt4"))
    
    ac_energy_out = parse_number(vrm_data.get("ac_out_energy_tot")) # Énergie AC sortie MultiPlus
    pv_energy_tot = parse_number(vrm_data.get("pv_yield_tot"))      # Énergie cumulée totale 4 MPPT
    
    rows.append({
        "date": date_str,
        "device": "inverter",
        "serial": "CUSTOM1",
        # MPPT 1
        "current.mppt.1": c1, "power.mppt.1": p1, "volt.mppt.1": v1, "energy.mppt.1": energy_mppt1,
        # MPPT 2
        "current.mppt.2": c2, "power.mppt.2": p2, "volt.mppt.2": v2, "energy.mppt.2": energy_mppt2,
        # MPPT 3
        "current.mppt.3": c3, "power.mppt.3": p3, "volt.mppt.3": v3, "energy.mppt.3": energy_mppt3,
        # MPPT 4
        "current.mppt.4": c4, "power.mppt.4": p4, "volt.mppt.4": v4, "energy.mppt.4": energy_mppt4,
        # Sortie Onduleur AC
        "power": ac_power_out,
        "volt": ac_volt_out,
        "current": ac_curr_out,
        "energy": ac_energy_out,   # Énergie AC fournie aux consommateurs (sortie MultiPlus)
        "energy_tot": pv_energy_tot, # Énergie DC globale générée par les 4 MPPT
        # Entrée Réseau AC-In (MultiPlus)
        "power_in": ac_power_in,
        "volt_in": ac_volt_in,
        "current_in": ac_curr_in,
        "state_of_charge": "",
        "temperature": "",
        "capacity": ""
    })

    # 2. Ligne Batterie BATTERIE1
    rows.append({
        "date": date_str,
        "device": "battery",
        "serial": "BATTERIE1",
        "current.mppt.1": "", "power.mppt.1": "", "volt.mppt.1": "","energy.mppt.1": "",
        "current.mppt.2": "", "power.mppt.2": "", "volt.mppt.2": "","energy.mppt.2": "",
        "current.mppt.3": "", "power.mppt.3": "", "volt.mppt.3": "","energy.mppt.3": "",
        "current.mppt.4": "", "power.mppt.4": "", "volt.mppt.4": "","energy.mppt.4": "",
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

    print(f" Génération du fichier unique réussie à {date_str} ({len(rows)} lignes).")
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

    print(f" Transfert du fichier vers le serveur SFTP : '{remote_target}'...")
    
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

def get_french_now_rounded_5min():
    """
    Renvoie l'heure française arrondie à la tranche de 5 minutes inférieure
    Exemple : 12:04:35 -> 12:00:00 | 12:07:12 -> 12:05:00
    """
    now = datetime.now(TZ_FRANCE)
    # Arrondi de la minute au multiple de 5 inférieur
    rounded_minute = (now.minute // 5) * 5
    return now.replace(minute=rounded_minute, second=0, microsecond=0)

# ------------------------------------------------------------------
# DÉCLENCHEMENT
# ------------------------------------------------------------------
if __name__ == "__main__":
    try:
        # Récupère l'heure arrondie à la tranche de 5 min (ex: 12:00:00)
        now_fr = get_french_now_rounded_5min()
        filename = generate_dynamic_filename(now_fr)
        
        print(f"1. Récupération du point instantané pour le creneau {now_fr.strftime('%H:%M:%S')}...")
        abs_file_path = fetch_instantaneous_and_build_csv(now_fr, filename)

        print("2. Envoi SFTP...")
        upload_via_sftp(abs_file_path, SFTP_REMOTE_DIR)

        if os.path.exists(abs_file_path):
            os.remove(abs_file_path)
            
    except Exception as e:
        print(f" Erreur : {e}")
        exit(1)
