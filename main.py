import os
import csv
import requests
import paramiko
from datetime import datetime

# ------------------------------------------------------------------
# 1. CONFIGURATION (Secrets / Variables d'environnement)
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
# 2. RÉCUPÉRATION DES DONNÉES DIAGNOSTIQUES VICTRON VRM
# ------------------------------------------------------------------
def fetch_victron_diagnostics():
    """
    Récupère le diagnostic complet de tous les équipements connectés sur le site VRM
    """
    url = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/diagnostics"
    headers = {
        "x-authorization": f"Token {VRM_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()

# ------------------------------------------------------------------
# 3. EXTRACTION ET FORMATAGE CONFORME S4E POWER API
# ------------------------------------------------------------------
def parse_and_format_s4e(diagnostics_data, output_path):
    records = diagnostics_data.get("records", [])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Dictionnaire temporaire pour grouper par appareil : (device_type, serial) -> {metric: value}
    devices_data = {}

    for rec in records:
        # Description Victron du composant
        desc = str(rec.get("description", "")).lower()
        code = str(rec.get("code", ""))
        formatted_val = rec.get("formattedValue", rec.get("rawValue", ""))
        
        # Nettoyage des valeurs numériques si nécessaire
        try:
            val = float(str(formatted_val).split()[0].replace(',', '.'))
        except (ValueError, TypeError, IndexError):
            val = formatted_val

        # Identification du numéro de série / identifiant de l'équipement
        # Utilisation de l'id de composant Victron s'il existe, sinon fallback sur l'ID de site
        device_id = str(rec.get("instance", rec.get("idSite", VRM_SITE_ID)))

        # A. REGULATEURS SOLAIRES (MPPT)
        if "solarcharger" in desc or "mppt" in desc or "pv charger" in desc:
            key = ("mppt", f"mppt_{device_id}")
            if key not in devices_data:
                devices_data[key] = {}
            
            if code in ["P", "PPV", "PvP"]:  # Puissance
                devices_data[key]["power"] = val
            elif code in ["V", "PVV", "PvV"]: # Tension
                devices_data[key]["volt"] = val
            elif code in ["I", "PVI"]: # Courant
                devices_data[key]["current"] = val

        # B. CONVERTISSEURS / ONDULEURS (MultiPlus / Quattro / Inverter)
        elif "vebus" in desc or "inverter" in desc or "multi" in desc or "quattro" in desc:
            key = ("converter", f"inv_{device_id}")
            if key not in devices_data:
                devices_data[key] = {}

            if code in ["P", "OutP", "AcP"]:      # Puissance sortie W
                devices_data[key]["power"] = val
            elif code in ["V", "OutV", "AcV"]:    # Tension sortie V
                devices_data[key]["volt"] = val
            elif code in ["I", "OutI", "AcI"]:    # Courant sortie A
                devices_data[key]["current"] = val
            elif code in ["InP", "GridP"]:        # Puissance entrée W
                devices_data[key]["power_in"] = val
            elif code in ["InV", "GridV"]:        # Tension entrée V
                devices_data[key]["volt_in"] = val
            elif code in ["InI", "GridI"]:        # Courant entrée A
                devices_data[key]["current_in"] = val

        # C. BATTERIES / BMS / BMV
        elif "battery" in desc or "bms" in desc or "bmv" in desc:
            key = ("battery", f"batt_{device_id}")
            if key not in devices_data:
                devices_data[key] = {}

            if code in ["SOC", "BSOC"]:           # Etat de charge %
                devices_data[key]["state_of_charge"] = val
            elif code in ["BV", "V", "BattV"]:    # Tension V
                devices_data[key]["volt"] = val
            elif code in ["BI", "I", "BattI"]:    # Courant A
                devices_data[key]["current"] = val
            elif code in ["BP", "P", "BattP"]:    # Puissance W
                devices_data[key]["power"] = val
            elif code in ["BT", "T", "BattT"]:    # Température °C
                devices_data[key]["temperature"] = val

    # Construction des lignes CSV au format S4E Simple
    # Colonnes autorisées selon variables_export.xlsx
    headers = [
        "date", "device", "serial", 
        "power", "volt", "current", 
        "power_in", "volt_in", "current_in", 
        "state_of_charge", "temperature"
    ]

    rows = []
    for (device_type, serial), metrics in devices_data.items():
        row = {
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
        }
        rows.append(row)

    # Si aucun équipement spécifique n'a pu être isolé, création d'une ligne de secours au niveau "site"
    if not rows:
        rows.append({
            "date": now_str,
            "device": "site",
            "serial": VRM_SITE_ID,
            "power": "", "volt": "", "current": "",
            "power_in": "", "volt_in": "", "current_in": "",
            "state_of_charge": "", "temperature": ""
        })

    # Écriture du fichier CSV respectant les contraintes S4E
    with open(output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f" Fichier CSV généré avec {len(rows)} équipement(s) extrait(s).")

# ------------------------------------------------------------------
# 4. ENVOI PAR SFTP SUR LE SERVEUR S4E
# ------------------------------------------------------------------
def upload_via_sftp(local_path, remote_path):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        ssh.connect(
            hostname=SFTP_HOST,
            port=SFTP_PORT,
            username=SFTP_USER,
            password=SFTP_PASS,
            look_for_keys=False,
            allow_agent=False,
            timeout=10
        )
        
        sftp = ssh.open_sftp()
        clean_path = remote_path.lstrip('/') if not remote_path.startswith('./') else remote_path
        
        print(f"Transfert vers '{clean_path}'...")
        sftp.put(local_path, clean_path)
        
        sftp.close()
        ssh.close()
        print(" Transfert SFTP réussi !")

    except Exception as e:
        print(f" Échec du transfert SFTP : {e}")
        raise

# ------------------------------------------------------------------
# EXÉCUTION
# ------------------------------------------------------------------
if __name__ == "__main__":
    try:
        print("1. Récupération des diagnostics Victron...")
        data = fetch_victron_diagnostics()
        
        print("2. Génération du CSV S4E Power API...")
        parse_and_format_s4e(data, LOCAL_FILE_PATH)
        
        print("3. Envoi SFTP...")
        upload_via_sftp(LOCAL_FILE_PATH, SFTP_REMOTE_PATH)
        
        print(" Tâche terminée avec succès.")
    except Exception as e:
        print(f" Erreur : {e}")
        exit(1)
