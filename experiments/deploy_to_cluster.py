import os
import sys
import paramiko

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

HOST = os.environ.get("LAB_HOST", "10.1.75.79")
USERNAME = os.environ.get("LAB_USER", "student")
PASSWORD = os.environ.get("LAB_PASSWORD", "REDACTED")

NODES = [
    {"name": "Sys1 (Load Balancer Server)", "ssh_port": 2237, "internal_ip": "172.17.0.38"},
    {"name": "Sys2 (Backend Server 1)",     "ssh_port": 2238, "internal_ip": "172.17.0.39"},
    {"name": "Sys3 (Backend Server 2)",     "ssh_port": 2239, "internal_ip": "172.17.0.40"},
    {"name": "Sys4 (Backend Server 3)",     "ssh_port": 2240, "internal_ip": "172.17.0.41"},
]

LOCAL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REMOTE_ROOT = "/home/student/Load_Balancer"

def upload_directory(sftp, local_dir, remote_dir):
    try:
        sftp.mkdir(remote_dir)
    except IOError:
        pass
        
    for item in os.listdir(local_dir):
        if item in [".git", "__pycache__", ".pytest_cache", "venv", ".idea", ".vscode"]:
            continue
        local_path = os.path.join(local_dir, item)
        remote_path = f"{remote_dir}/{item}".replace("\\", "/")
        
        if os.path.isdir(local_path):
            upload_directory(sftp, local_path, remote_path)
        else:
            sftp.put(local_path, remote_path)

def deploy_to_node(node):
    print(f"\nConnecting to {node['name']} on {HOST}:{node['ssh_port']}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client.connect(HOST, port=node["ssh_port"], username=USERNAME, password=PASSWORD, timeout=10)
        sftp = client.open_sftp()
        
        try:
            sftp.mkdir(REMOTE_ROOT)
        except IOError:
            pass
            
        print(f"  Syncing project files to {REMOTE_ROOT}...")
        
        for sub_item in ["src", "config", "experiments", "tests", "requirements.txt", "README.md", "LAB_REPORT.md", ".gitignore"]:
            local_item_path = os.path.join(LOCAL_ROOT, sub_item)
            remote_item_path = f"{REMOTE_ROOT}/{sub_item}"
            if os.path.exists(local_item_path):
                if os.path.isdir(local_item_path):
                    upload_directory(sftp, local_item_path, remote_item_path)
                else:
                    sftp.put(local_item_path, remote_item_path)
        
        sftp.close()
        
        stdin, stdout, stderr = client.exec_command(f"find {REMOTE_ROOT} -maxdepth 2 -not -path '*/.*'")
        files = stdout.read().decode("utf-8").strip().splitlines()
        print(f"  Uploaded successfully! Remote files count: {len(files)}")
        
        client.close()
    except Exception as e:
        print(f"  Error deploying to {node['name']}: {e}")

def main():
    print("=" * 70)
    print("DEPLOYING LOAD BALANCER PROJECT TO DISTRIBUTED LAB CLUSTER")
    print(f"Target Host: {HOST}")
    print("=" * 70)
    
    for node in NODES:
        deploy_to_node(node)
        
    print("\n" + "=" * 70)
    print("ALL FILES DEPLOYED TO SYS1, SYS2, SYS3, SYS4 SUCCESSFULLY!")
    print("=" * 70 + "\n")

if __name__ == "__main__":
    main()
