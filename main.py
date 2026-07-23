import os
import json
import csv
import requests
import paramiko

# ------------------------------------------------------------------
# 1. CONFIGURATION (Variables d'environnement pour la sécurité)
# ------------------------------------------------------------------
VRM_API_TOKEN = os.getenv("VRM_API_TOKEN")
VRM_SITE_ID = os.getenv("VRM_SITE_ID")

SFTP_HOST = os.getenv("SFTP_HOST")
SFTP_PORT = int(os.getenv("SFTP_PORT", 22))
SFTP_USER = os.getenv("SFTP_USER")
SFTP_PASS = os.getenv("SFTP_PASS")
SFTP_REMOTE_PATH = os.getenv("SFTP_REMOTE_PATH", "/upload/solar_data.csv")

LOCAL_FILE_PATH = "solar_data.csv"

# ------------------------------------------------------------------
# 2. RÉCUPÉRATION DES DONNÉES VICTRON VRM
# ------------------------------------------------------------------
def fetch_victron_data():
    # End-point diagnostic / statistiques de l'installation Victron
    url = f"https://vrmapi.victronenergy.com/v2/installations/{VRM_SITE_ID}/diagnostics"
    headers = {
        "x-authorization": f"Token {VRM_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()

# ------------------------------------------------------------------
# 3. TRANSFORMATION / FORMATAGE
# ------------------------------------------------------------------
def format_data_to_csv(data, output_path):
    records = data.get("records", [])
    
    # Exemple : Extraction de quelques métriques clés
    # Adaptez la logique de filtrage selon vos besoins et le format SaaS requis
    extracted = []
    for item in records:
        code = item.get("code")
        value = item.get("formattedValue", item.get("rawValue"))
        timestamp = item.get("timestamp")
        
        if code in ["SOC", "Pdc", "I", "V"]:  # Batterie, Puissance Solar, Courant, Tension
            extracted.append({"timestamp": timestamp, "metric": code, "value": value})

    # Écriture dans le fichier CSV au format demandé
    with open(output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "metric", "value"])
        writer.writeheader()
        writer.writerows(extracted)

# ------------------------------------------------------------------
# 4. ENVOI PAR SFTP
# ------------------------------------------------------------------
def upload_via_sftp(local_path, remote_path):
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=SFTP_USER, password=SFTP_PASS)
    
    sftp = paramiko.SFTPClient.from_transport(transport)
    sftp.put(local_path, remote_path)
    
    sftp.close()
    transport.close()

# ------------------------------------------------------------------
# PIPELINE PRINCIPAL
# ------------------------------------------------------------------
if __name__ == "__main__":
    try:
        print("1. Récupération des données VRM...")
        raw_data = fetch_victron_data()
        
        print("2. Formattage du fichier...")
        format_data_to_csv(raw_data, LOCAL_FILE_PATH)
        
        print("3. Envoi vers le serveur SaaS via SFTP...")
        upload_via_sftp(LOCAL_FILE_PATH, SFTP_REMOTE_PATH)
        
        print(" Succès de la synchronisation.")
    except Exception as e:
        print(f" Erreur lors de l'exécution : {e}")
        exit(1)
