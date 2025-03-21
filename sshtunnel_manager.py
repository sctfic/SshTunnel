import os
import json
import subprocess
import platform
import shutil
import logging
import argparse
import socket
import time
import sys
import signal
import inspect
from typing import Optional, List, Dict, Any

# Constantes pour les chemins
CONFIG_DIR = "/etc/sshtunnel/conf.d"
LOG_DIR = "/var/log/sshtunnel"
PID_DIR = "/run/sshtunnel"

start_time = time.time()

# Liste des dépendances système requises
DEPENDENCIES = ['ssh', 'ping', 'netstat', 'nc', 'ssh-keygen', 'trickle', 'autossh', 'sshpass']

# Configuration du logging
logging.basicConfig(
    filename=os.path.join(LOG_DIR, "sshtunnel.log"),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def logline(message):
    """Affiche le message précédé du numéro de la ligne où cette fonction a été appelée."""
    frame = inspect.currentframe().f_back
    line_number = frame.f_lineno
    print(f"[{line_number}]  {message}")

def check_dependencies() -> None:
    """
    Vérifie si toutes les dépendances nécessaires sont installées.
    Quitte avec un code d'erreur si une dépendance manque.
    """
    missing = [dep for dep in DEPENDENCIES if shutil.which(dep) is None]
    if missing:
        error_msg = f"Dépendances manquantes : {', '.join(missing)}. Installez-les avec votre gestionnaire de paquets."
        logging.error(error_msg)
        raise SystemExit(error_msg)

def check_root() -> None:
    """Vérifie si le script est exécuté avec des privilèges root."""
    if os.getuid() != 0:
        error_msg = "Ce script doit être exécuté avec des privilèges root (sudo)."
        logging.error(error_msg)
        raise SystemExit(error_msg)

def check_dirs(config_dir: str = CONFIG_DIR, log_dir: str = LOG_DIR, pid_dir: str = PID_DIR) -> None:
    """
    Crée les répertoires nécessaires s'ils n'existent pas et définit les permissions à 750.
    """
    for directory in [config_dir, log_dir, pid_dir]:
        try:
            os.makedirs(directory, exist_ok=True)
            os.chmod(directory, 0o750)
            logging.info(f"Répertoire {directory} vérifié ou créé avec permissions 750.")
        except OSError as e:
            logging.error(f"Erreur lors de la création du répertoire {directory} : {e}")
            raise SystemExit(1)

def validate_config(config):
    required_fields = ["user", "ip", "ssh_port", "ssh_key", "tunnels"]
    for field in required_fields:
        if field not in config:
            raise ValueError(f"Champ obligatoire manquant : {field}")
    
    for tunnel_type, tunnels in config["tunnels"].items():
        for port, tunnel in tunnels.items():
            if "name" not in tunnel:
                raise ValueError(f"Chaque tunnel doit avoir un 'name'")
            if tunnel_type == "-L" and not all(k in tunnel for k in ["listen_port", "endpoint_host", "endpoint_port"]):
                raise ValueError("Champs manquants pour tunnel de type -L (listen_port, endpoint_host, endpoint_port)")
            elif tunnel_type == "-R" and not all(k in tunnel for k in ["listen_host", "listen_port", "endpoint_host", "endpoint_port"]):
                raise ValueError("Champs manquants pour tunnel de type -R (listen_host, listen_port, endpoint_host, endpoint_port)")
            elif tunnel_type == "-D" and "listen_port" not in tunnel:
                raise ValueError("Champs manquants pour tunnel de type -D (listen_port)")
    return True

def start_tunnel(config_name):
    logline(config_name)
    """Démarre les tunnels SSH définis dans la configuration."""
    pid_file = os.path.join(PID_DIR, f"{config_name}.pid")
    log_file = os.path.join(LOG_DIR, f"{config_name}.log")

    # Vérifier si le tunnel est déjà en cours d'exécution
    if os.path.exists(pid_file):
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)  # Vérifier si le processus est toujours actif
            logging.info(f"Tunnel {config_name} déjà en cours d'exécution avec PID {pid}")
            return
        except OSError:
            logging.warning(f"Fichier PID trouvé mais le processus {pid} n'est pas actif. Redémarrage du tunnel.")

    with open(f"{CONFIG_DIR}/{config_name}.json", "r") as f:
        config = json.load(f)
    validate_config(config)  # Validation préalable

    # Construction de la commande de base avec autossh
    cmd = ["autossh", "-M", "0", "-N", "-i", config["ssh_key"], 
           f"{config['user']}@{config['ip']}", "-p", str(config["ssh_port"])]

    # Ajout des options avancées si présentes
    if "options" in config and "keepalive_interval" in config["options"]:
        cmd += ["-o", f"ServerAliveInterval={config['options']['keepalive_interval']}"]

    for tunnel_type, tunnels in config["tunnels"].items():
        for port, tunnel in tunnels.items():
            if tunnel_type == "-L":
                cmd += [f"-L {tunnel['listen_port']}:{tunnel['endpoint_host']}:{tunnel['endpoint_port']}"]
            elif tunnel_type == "-R":
                cmd += [f"-R {tunnel['listen_host']}:{tunnel['listen_port']}:{tunnel['endpoint_host']}:{tunnel['endpoint_port']}"]
            elif tunnel_type == "-D":
                cmd += [f"-D {tunnel['listen_port']}"]

    # Application de la limitation de bande passante si définie
    if "bandwidth" in config:
        cmd = ["trickle", "-u", str(config["bandwidth"]["up"]), "-d", str(config["bandwidth"]["down"])] + cmd

    logline(cmd)

    # Lancement du processus avec redirection des sorties
    with open(log_file, "a") as log:
        process = subprocess.Popen(cmd, stdout=log, stderr=log)
        with open(pid_file, "w") as f:
            f.write(str(process.pid))
        logging.info(f"Tunnel {config_name} démarré avec PID {process.pid}")

def stop_tunnel(config_name):
    logline(config_name)
    """Arrête tous les tunnels associés à une configuration donnée."""
    config_path = f"{CONFIG_DIR}/{config_name}.json"
    if not os.path.exists(config_path):
        logging.error(f"Configuration {config_name} introuvable.")
        return
    with open(config_path, "r") as f:
        config = json.load(f)

    stopped = False
    for pid_file in os.listdir(PID_DIR):
        # verifier que le nom de pid_file est egal a config_name
        if pid_file.startswith(f"{config_name}"):
            pid_path = os.path.join(PID_DIR, pid_file)
            try:
                with open(pid_path, "r") as f:
                    pid = int(f.read().strip())
                os.kill(pid, 15)  # SIGTERM
                os.remove(pid_path)
                logging.info(f"Tunnel {pid_file} arrêté avec succès (PID {pid})")
                stopped = True
            except (ProcessLookupError, ValueError) as e:
                logging.warning(f"Tunnel {pid_file} déjà terminé ou PID invalide : {e}")
                os.remove(pid_path)
            except OSError as e:
                logging.error(f"Erreur système lors de l'arrêt de {pid_file} : {e}")
    if not stopped:
        logging.info(f"Aucun tunnel actif trouvé pour {config_name}")

def add_tunnel(config_name, tunnel_name, tunnel_type, params):
    """Ajoute un nouveau tunnel à une configuration JSON."""
    logline(config_name)
    logline(tunnel_name)
    logline(tunnel_type)
    logline(params)
    config_path = f"{CONFIG_DIR}/{config_name}.json"
    logline(config_path)
    if not os.path.exists(config_path):
        logging.error(f"Configuration {config_name} introuvable.")
        return
    with open(config_path, "r") as f:
        config = json.load(f)
    
    # Validation et création du nouveau tunnel
    if tunnel_type == "-L":
        if len(params) != 3:
            raise ValueError("Usage: -L listen_port endpoint_host endpoint_port")
        new_tunnel = {"name": tunnel_name, "listen_port": params[0], "endpoint_host": params[1], "endpoint_port": params[2]}
    elif tunnel_type == "-R":
        if len(params) != 4:
            raise ValueError("Usage: -R listen_host listen_port endpoint_host endpoint_port")
        new_tunnel = {"name": tunnel_name, "listen_host": params[0], "listen_port": params[1], "endpoint_host": params[2], "endpoint_port": params[3]}
    elif tunnel_type == "-D":
        if len(params) != 1:
            raise ValueError("Usage: -D listen_port")
        new_tunnel = {"name": tunnel_name, "listen_port": params[0]}
    else:
        raise ValueError("Type de tunnel invalide")
    
    if tunnel_type not in config["tunnels"]:
        config["tunnels"][tunnel_type] = {}
    config["tunnels"][tunnel_type][params[0]] = new_tunnel

    with open(config_path, "w") as f:
        json.dump(config, f, indent=4)
    logging.info(f"Tunnel {tunnel_name} ajouté à {config_path}")

def remove_tunnel(config_name, tunnel_name):
    """Supprime tous les tunnels spécifiques d'une configuration ayant le même nom."""
    config_path = f"{CONFIG_DIR}/{config_name}.json"
    if not os.path.exists(config_path):
        logging.error(f"Configuration {config_name} introuvable.")
        return
    with open(config_path, "r") as f:
        config = json.load(f)
    
    found = False
    for tunnel_type, tunnels in config["tunnels"].items():
        for port, tunnel in list(tunnels.items()):
            if tunnel["name"] == tunnel_name:
                del config["tunnels"][tunnel_type][port]
                found = True
    
    if found:
        with open(config_path, "w") as f:
            json.dump(config, f, indent=4)
        logging.info(f"Tous les tunnels nommés {tunnel_name} ont été supprimés de {config_path}")
    else:
        logging.info(f"Aucun tunnel nommé {tunnel_name} trouvé dans {config_path}")

def pairing(ip, admin_user, password, config_name, bandwidth=None):
    key_path = f"/root/.ssh/{config_name}_key"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", ""])
    os.chmod(key_path, 0o600)
    
    # Créer l'utilisateur distant
    ssh_cmd = f"sshpass -p {password} ssh {admin_user}@{ip} 'useradd -m -s /bin/false tunnel_user && mkdir -p ~tunnel_user/.ssh && cat >> ~tunnel_user/.ssh/authorized_keys'"
    subprocess.run(ssh_cmd, shell=True, input=open(f"{key_path}.pub").read(), text=True)
    
    # Générer le fichier JSON
    config = {
        "user": "tunnel_user",
        "ip": ip,
        "ssh_port": 22,
        "ssh_key": key_path,
        "tunnels": []
    }
    if bandwidth:
        up, down = bandwidth.split("/")
        config["bandwidth"] = {"up": int(up), "down": int(down)}
    
    with open(f"{CONFIG_DIR}/{config_name}.json", "w") as f:
        json.dump(config, f, indent=4)


def is_port_open(ip, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)  # Timeout de 1 seconde
    start_time = time.time()  # Temps de début
    try:
        sock.connect((ip, port))
        return round((time.time() - start_time) * 1000, 1)  # Convertir en millisecondes
    except (socket.timeout, socket.error):
        return None  # Retourne None si la connexion échoue
    finally:
        sock.close()

def is_host_reachable(ip):
    """Teste si une adresse IP est joignable via ICMP (ping)."""
    command = ["ping", "-c", "3", "-W", "1", "-i", "0.01", ip]
    start_time = time.time()  # Temps de début
    try:
        output = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        return round((time.time() - start_time) * 1000, 1)  # Convertir en millisecondes
        # return output.returncode == 0  # 0 signifie succès, donc l'IP répond au ping
    except subprocess.TimeoutExpired:
        return None  # Timeout = l'IP ne répond pas
    except Exception as e:
        print(f"Erreur lors du ping: {e}")
        return None

def is_port_listening(ip, port):
    """Vérifie si un port est en écoute sur l'IP donnée."""
    try:
        output = subprocess.check_output(["netstat", "-tln"], text=True)
        for line in output.splitlines():
            if f"{ip}:{port}" in line and "LISTEN" in line:
                return True
        return False
    except subprocess.CalledProcessError:
        return False

def check_status(config_name=None):
    """
    Vérifie l'état des tunnels SSH selon le cahier des charges.
    - Si config_name est None : check simplifié de tous les sites géographiques.
    - Sinon : check complet pour la configuration spécifiée.
    """
    if not config_name:  # Check simplifié de tous les sites
        result = {"servers": []}
        for conf_file in os.listdir(CONFIG_DIR):
            if conf_file.endswith(".json"):
                with open(os.path.join(CONFIG_DIR, conf_file)) as f:
                    config = json.load(f)
                ip = config["ip"]
                ssh_port = config["ssh_port"]
                name = conf_file[:-5]  # Retirer .json

                # Tester d'abord le port SSH
                port_latency = is_port_open(ip, ssh_port)
                if port_latency is not None:
                    # Si le port est ouvert, réutiliser la latence pour ping_ms
                    ping_ms = port_latency
                    port_status = True
                else:
                    # Sinon, tester avec ping
                    ping_ms = is_host_reachable(ip)
                    port_status = False

                status = {
                    "name": name,
                    "icmp": {"ip": ip, "status": ping_ms is not None, "latency_ms": ping_ms},
                    "tcp": {"port": ssh_port, "status": port_status, "latency_ms": port_latency}
                }
                result["servers"].append(status)
        return json.dumps(result, indent=4)

    else:  # Check complet pour une config spécifique
        config_path = os.path.join(CONFIG_DIR, f"{config_name}.json")
        if not os.path.exists(config_path):
            return json.dumps({"error": f"Configuration {config_name} introuvable."}, indent=4)

        with open(config_path) as f:
            config = json.load(f)

        ip = config["ip"]
        ssh_port = config["ssh_port"]

        # Tester d'abord le port SSH
        port_latency = is_port_open(ip, ssh_port)
        if port_latency is not None:
            ping_ms = port_latency
            port_status = True
        else:
            ping_ms = is_host_reachable(ip)
            port_status = False

        result = {
            "servers": [{
                "name": config_name,
                "icmp": {"host": ip, "status": ping_ms is not None, "latency_ms": ping_ms},
                "tcp": {"port": ssh_port, "status": port_status, "latency_ms": port_latency}
            }],
            "tunnels": []
        }

        # Vérification des tunnels
        for tunnel_type, tunnels in config["tunnels"].items():
            for port, tunnel in tunnels.items():
                tunnel_status = {"name": tunnel["name"]}

                # Gestion des listen_port selon le type de tunnel
                if tunnel_type in ["-L", "-D"]:
                    listen_port = int(tunnel["listen_port"])
                    # Vérifier localement si le port est en écoute
                    listen_port_status = is_port_listening("0.0.0.0", listen_port)
                    tunnel_status["listen_port"] = {
                        "port": listen_port,
                        "status": listen_port_status,
                        "latency_ms": 1  # Pas de latence pour l'état d'écoute local
                    }
                elif tunnel_type == "-R":
                    # Tester le listen_port sur listen_host
                    listen_host = tunnel["listen_host"]
                    listen_port = int(tunnel["listen_port"])
                    listen_port_latency = is_port_open(listen_host, listen_port)
                    tunnel_status["listen_port"] = {
                        "port": listen_port,
                        "status": listen_port_latency is not None,
                        "latency_ms": listen_port_latency
                    }
                    # Vérifier listen_host si listen_port est fermé
                    if listen_port_latency is None:
                        tunnel_status["listen_host"] = {"host": listen_host, "latency_ms": is_host_reachable(listen_host)}
                    else:
                        tunnel_status["listen_host"] = {"host": listen_host, "latency_ms": listen_port_latency}

                # Gestion des endpoint_port pour -L et -R
                if tunnel_type in ["-L", "-R"]:
                    endpoint_host = tunnel["endpoint_host"]
                    endpoint_port = int(tunnel["endpoint_port"])
                    endpoint_port_latency = is_port_open(endpoint_host, endpoint_port)
                    tunnel_status["endpoint_port"] = {
                        "port": endpoint_port,
                        "status": endpoint_port_latency is not None,
                        "latency_ms": endpoint_port_latency
                    }
                    # Vérifier endpoint_host si endpoint_port est fermé
                    if endpoint_port_latency is None:
                        tunnel_status["endpoint_host"] = {"host": endpoint_host, "latency_ms": is_host_reachable(endpoint_host)}
                    else:
                        tunnel_status["endpoint_host"] = {"host": endpoint_host, "latency_ms": endpoint_port_latency}

                result["tunnels"].append(tunnel_status)

        return json.dumps(result, indent=4)

def restart_tunnel(config_name):
    """Recharge les configurations et redémarre les tunnels."""
    stop_tunnel(config_name)
    start_tunnel(config_name)
    logging.info("Configurations rechargées et tunnels redémarrés.")

def status_service():
    """Renvoie l'état de tous les processus actifs"""
    result = {"tunnels": []}
    for pid_file in os.listdir(PID_DIR):
        config_name = pid_file[:-4]
        with open(os.path.join(PID_DIR, pid_file), "r") as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)  # Vérifier si le processus est toujours actif
            result["tunnels"].append({"name": config_name, "status": "active", "pid": pid})
        except OSError:
            result["tunnels"].append({"name": config_name, "status": "inactive"})
    return json.dumps(result, indent=4)

def signal_handler(sig, frame):
    """Gestionnaire de signaux pour arrêter proprement le service"""
    logging.info(f"Signal {sig} reçu, arrêt du service...")
    
    # Arrêter tous les tunnels
    for config_file in os.listdir(CONFIG_DIR):
        if config_file.endswith(".json"):
            config_name = config_file[:-5]
            stop_tunnel(config_name)
    
    sys.exit(0)

# Enregistrer les gestionnaires de signaux
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


if __name__ == "__main__":
    # Vérifications initiales
    check_dependencies()
    check_root()
    
    # Analyser les arguments de la ligne de commande
    parser = argparse.ArgumentParser(description="Gestionnaire de tunnels SSH")
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "pairing", "check", "add", "remove"])
    parser.add_argument("config", nargs="?", help="Nom de la configuration")
    parser.add_argument("tunnel", nargs="?", help="Nom du tunnel")
    parser.add_argument("type", nargs="?", help="Type du tunnel")
    parser.add_argument("params", nargs="*", help="Paramètres du tunnel")

    parser.add_argument("-i", "--ip", help="Adresse IP pour le pairing")
    parser.add_argument("-u", "--user", help="Nom d'utilisateur pour le pairing")
    parser.add_argument("-p", "--password", help="Mot de passe pour le pairing")
    parser.add_argument("-b", "--bandwidth", help="Limites de bande passante (up/down)")
    args = parser.parse_args()

    # Exécuter la commande appropriée
    if args.command == "start":
        if args.config:
            start_tunnel(args.config)
        else:
            for config_file in os.listdir(CONFIG_DIR):
                if config_file.endswith(".json"):
                    start_tunnel(config_file[:-5])
    elif args.command == "stop":
        if args.config:
            stop_tunnel(args.config)
        else:
            for config_file in os.listdir(CONFIG_DIR):
                if config_file.endswith(".json"):
                    stop_tunnel(config_file[:-5])
    elif args.command == "restart":
        if args.config:
            restart_tunnel(args.config)
        else:
            for config_file in os.listdir(CONFIG_DIR):
                if config_file.endswith(".json"):
                    config_name = config_file[:-5]
                    restart_tunnel(config_name)
    elif args.command == "status":
        print(status_service())
    elif args.command == "pairing":
        if not all([args.ip, args.user, args.password, args.config]):
            print("Tous les paramètres sont requis pour le pairing")
        else:
            pairing(args.ip, args.user, args.password, args.config, args.bandwidth)
    elif args.command == "check":
        print(check_status(args.config))
    elif args.command == "add":
        if not args.config or not args.tunnel or not args.type or not args.params:
            print("Tous les paramètres sont requis pour ajouter un tunnel")
        else:
            add_tunnel(args.config, args.tunnel, f"-{args.type}", args.params)
            restart_tunnel(args.config)
    elif args.command == "remove":
        if not args.config or not args.tunnel:
            print("Les paramètres config et tunnel sont requis pour supprimer un tunnel")
        else:
            remove_tunnel(args.config, args.tunnel)
            restart_tunnel(args.config)
    else:
        print("Commande invalide")


    # Calculer la durée d'exécution
    duration = time.time() - start_time
    # Afficher la durée d'exécution
    print(f"Durée : {duration:.2f} sec")