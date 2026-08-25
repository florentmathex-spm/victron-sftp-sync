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

# --- LISTE DES SERVEURS SFTP ---
SFTP_SERVERS = [
    {
        "name": "SFTP 1 (Actuel)",
        "host": os.getenv("SFTP_HOST"),
        "port": int(os.getenv("SFTP_PORT", 22)),
        "user": os.getenv("SFTP_USER"),
        "pass": os.getenv("SFTP_PASS"),
        "remote_dir": os.getenv("SFTP_REMOTE_PATH", "./")
    },
    {
        "name": "SFTP 2 (Solar-PM)",
        "host": os.getenv("SFTP2_HOST", "sftp.solar-pm.fr"),
        "port": int(os.getenv("SFTP2_PORT", 2222)),
        "user": os.getenv("SFTP2_USER", "centrale_user"),
        "pass": os.getenv("SFTP2_PASS", "password123"),
        "remote_dir": os.getenv("SFTP2_REMOTE_PATH", "./")
    }
]

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
# EXTRACTION & CONSTRUCTION DU POINT UNIQUE (CUSTOM1, METER1 & 1)
# ------------------------------------------------------------------
def fetch_instantaneous_and_build_csv(dt_now, output_file):
    headers_api = {
        "x-authorization": f"Token {VRM_API_TOKEN}",
        "Content-Type": "application/json"
    }

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

            elif "HQ2441TVZCW" in serial or "278" in inst_id:
                p3 = met.get("power", "")
                v3 = met.get("volt", "")
                c3 = met.get("current", calc_current(p3, v3))

                mppt_values[3] = {"power": p3, "volt": v3, "current": c3}
                if isinstance(p3, (int, float)): tot_pv_power += p3; has_pv_data = True

            else:
                p4 = met.get("power", "")
                v4 = met.get("volt", "")
                c4 = met.get("current", calc_current(p4, v4))

                mppt_values[4] = {"power": p4, "volt": v4, "current": c4}
                if isinstance(p4, (int, float)): tot_pv_power += p4; has_pv_data = True

        elif dev_type == "converter":
            inv_power = met.get("power", "")  # Puissance mesurée via l'API (MultiPlus)
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

    # Puissance AC calculée de l'onduleur CUSTOM1 = Somme MPPTs * 0.98
    calculated_custom1_power = round(tot_pv_power * 0.98, 2) if has_pv_data else ""

    power_limitation_pct = ""
    if isinstance(batt_soc, (int, float)):
        if batt_soc > 97.5:
            if isinstance(calculated_custom1_power, (int, float)):
                power_limitation_pct = round((calculated_custom1_power / 11700) * 100, 2)
            else:
                power_limitation_pct = ""
        else:
            power_limitation_pct = 100

    headers_csv = [
        "date", "device", "serial",
        "current.mppt.1", "power.mppt.1", "volt.mppt.1",
        "current.mppt.2", "power.mppt.2", "volt.mppt.2",
        "current.mppt.3", "power.mppt.3", "volt.mppt.3",
        "current.mppt.4", "power.mppt.4", "volt.mppt.4",
        "power", "volt", "current", "energy", "energy_tot",
        "power_in", "volt_in", "current_in",
        "state_of_charge", "temperature", "capacity",
        "power_limitation_pct"
    ]

    date_str = dt_now.strftime("%Y-%m-%d %H:%M:%S")
    rows = []

    # 1. ONDULEUR (CUSTOM1) -> Puissance issue des 4 MPPTs x 98%
    rows.append({
        "date": date_str,
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
        "power": calculated_custom1_power,
        "volt": inv_volt,
        "current": inv_current,
        "energy": "",
        "energy_tot": inv_energy_tot,
        "power_in": inv_power_in,
        "volt_in": inv_volt_in,
        "current_in": inv_current_in,
        "state_of_charge": "",
        "temperature": "",
        "capacity": "",
        "power_limitation_pct": power_limitation_pct
    })

    # 2. COMPTEUR DE MESURE (METER1) -> Puissance consommée mesurée via l'API Victron MultiPlus (inv_power)
    rows.append({
        "date": date_str,
        "device": "meter",
        "serial": "METER1",
        "current.mppt.1": "", "power.mppt.1": "", "volt.mppt.1": "",
        "current.mppt.2": "", "power.mppt.2": "", "volt.mppt.2": "",
        "current.mppt.3": "", "power.mppt.3": "", "volt.mppt.3": "",
        "current.mppt.4": "", "power.mppt.4": "", "volt.mppt.4": "",
        "power": inv_power,  # Ancienne valeur brute de custom1.power
        "volt": inv_volt,
        "current": inv_current,
        "energy": "",
        "energy_tot": inv_energy_tot,
        "power_in": "",
        "volt_in": "",
        "current_in": "",
        "state_of_charge": "",
        "temperature": "",
        "capacity": "",
        "power_limitation_pct": ""
    })

    # 3.  (BATTERIE1)
    rows.append({
        "date": date_str,
        "device": "battery",
        "serial": "BATTERY1",
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
        "capacity": batt_cap,
        "power_limitation_pct": ""
    })

    abs_output_path = os.path.abspath(output_file)
    with open(abs_output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers_csv, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f" Génération du fichier unique réussie à {date_str} ({len(rows)} lignes).")
    return abs_output_path

# ------------------------------------------------------------------
# ENVOI SFTP MULTI-SERVEURS
# ------------------------------------------------------------------
def upload_to_single_sftp(local_abs_path, server_config):
    name = server_config["name"]
    host = server_config["host"]
    port = server_config["port"]
    user = server_config["user"]
    password = server_config["pass"]
    remote_dir_config = server_config["remote_dir"]

    if not host or not user:
        print(f" Config manquante pour {name}, envoi ignoré.")
        return False

    filename = os.path.basename(local_abs_path)
    clean_dir = (remote_dir_config or "").strip().strip('/')

    if clean_dir in ["", "."]:
        remote_target = filename
    else:
        remote_target = f"{clean_dir}/{filename}"

    print(f" Transfert vers {name} ({host}:{port}) -> '{remote_target}'...")

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=host, port=port, username=user, password=password,
            look_for_keys=False, allow_agent=False, timeout=15
        )

        sftp = ssh.open_sftp()
        try:
            sftp.put(local_abs_path, remote_target)
            print(f" Transfert réussi sur {name} !")
            return True
        except PermissionError:
            print(f" ERREUR DROITS sur {name} ('{remote_target}'). Dépôt de secours à la racine...")
            sftp.put(local_abs_path, filename)
            print(f" Transfert de secours réussi sur {name} !")
            return True
        finally:
            sftp.close()
            ssh.close()

    except Exception as e:
        print(f" ÉCHEC du transfert vers {name} : {e}")
        return False

def upload_via_multi_sftp(local_abs_path):
    if not os.path.exists(local_abs_path):
        raise FileNotFoundError(f"Le fichier local '{local_abs_path}' est introuvable.")

    results = []
    for server in SFTP_SERVERS:
        res = upload_to_single_sftp(local_abs_path, server)
        results.append(res)
    
    return any(results)

def get_french_now_rounded_5min():
    now = datetime.now(TZ_FRANCE)
    rounded_minute = (now.minute // 5) * 5
    return now.replace(minute=rounded_minute, second=0, microsecond=0)

# ------------------------------------------------------------------
# DÉCLENCHEMENT
# ------------------------------------------------------------------
if __name__ == "__main__":
    try:
        now_fr = get_french_now_rounded_5min()
        filename = generate_dynamic_filename(now_fr)

        print(f"1. Récupération du point instantané pour le créneau {now_fr.strftime('%H:%M:%S')}...")
        abs_file_path = fetch_instantaneous_and_build_csv(now_fr, filename)

        print("2. Envoi vers les 2 serveurs SFTP...")
        upload_via_multi_sftp(abs_file_path)

        if os.path.exists(abs_file_path):
            os.remove(abs_file_path)

    except Exception as e:
        print(f" Erreur globale : {e}")
        exit(1)
