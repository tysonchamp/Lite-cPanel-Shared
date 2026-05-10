import os
import sys
import json
import subprocess
import tarfile
import time
import ftplib
import shutil
from datetime import datetime, timedelta

try:
    import boto3
except ImportError:
    boto3 = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'backup_config.json')
PASSWORDS_FILE = '/var/lib/lite-cpanel/.passwords'
BACKUP_DIR = '/backup'
TEMP_DIR = '/tmp/lite_cpanel_backups'

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
    sys.stdout.flush()

def get_settings():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            log(f"Error reading config: {e}")
    return {}

def get_mysql_password():
    password = ''
    if os.path.exists(PASSWORDS_FILE):
        with open(PASSWORDS_FILE, 'r') as f:
            for line in f:
                if line.startswith('MySQL Root Password:'):
                    password = line.split(':', 1)[1].strip()
                    break
    return password

def cleanup_local_backups(retention_days):
    if not os.path.exists(BACKUP_DIR):
        return
    
    log(f"Cleaning up local backups older than {retention_days} days...")
    cutoff = time.time() - (retention_days * 86400)
    for root, dirs, files in os.walk(BACKUP_DIR):
        for f in files:
            if f.endswith('.tar.gz') or f.endswith('.sql'):
                filepath = os.path.join(root, f)
                if os.stat(filepath).st_mtime < cutoff:
                    try:
                        os.remove(filepath)
                        log(f"Deleted old backup: {filepath}")
                    except Exception as e:
                        log(f"Failed to delete {filepath}: {e}")

def run_backup():
    settings = get_settings()
    if not settings:
        log("No backup settings found. Exiting.")
        return

    # 1. Setup
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR, exist_ok=True)
        
    mysql_pass = get_mysql_password()
    
    # 2. Dump MySQL databases individually
    log("Starting MySQL dumps...")
    try:
        mysql_cmd = ['mysql', '-u', 'root', '-N', '-B', '-e', 'SHOW DATABASES;']
        if mysql_pass:
            mysql_cmd.insert(3, f'-p{mysql_pass}')
        result = subprocess.run(mysql_cmd, capture_output=True, text=True, check=True)
        databases = [db.strip() for db in result.stdout.split('\n') if db.strip() and db.strip() not in ('information_schema', 'performance_schema', 'mysql', 'sys')]
        
        mysql_temp_dir = os.path.join(TEMP_DIR, 'mysql')
        os.makedirs(mysql_temp_dir, exist_ok=True)
        
        for db in databases:
            dump_cmd = ['mysqldump', '-u', 'root', db]
            if mysql_pass:
                dump_cmd.insert(3, f'-p{mysql_pass}')
                
            temp_sql_file = os.path.join(mysql_temp_dir, f"{db}-{timestamp}.sql")
            with open(temp_sql_file, 'w') as sql_out:
                subprocess.run(dump_cmd, stdout=sql_out, stderr=subprocess.PIPE, check=True)
            log(f"Dumped database: {db}")
    except Exception as e:
        log(f"MySQL backup failed: {e}")

    # 3. Create Tar Archives for each domain
    log("Creating archives for each domain...")
    if os.path.exists('/var/www'):
        for domain in os.listdir('/var/www'):
            domain_path = os.path.join('/var/www', domain)
            if os.path.isdir(domain_path) and domain != 'html':
                domain_temp_dir = os.path.join(TEMP_DIR, domain)
                os.makedirs(domain_temp_dir, exist_ok=True)
                
                temp_tar_file = os.path.join(domain_temp_dir, f"{domain}-{timestamp}.tar.gz")
                try:
                    with tarfile.open(temp_tar_file, "w:gz") as tar:
                        tar.add(domain_path, arcname=domain)
                    log(f"Archive created for domain: {domain}")
                except Exception as e:
                    log(f"Archive creation failed for {domain}: {e}")

    # 4. Storage Handling
    if settings.get('local_enabled', True):
        log("Saving to local /backup directory...")
        for root, dirs, files in os.walk(TEMP_DIR):
            for f in files:
                rel_dir = os.path.relpath(root, TEMP_DIR)
                dest_dir = os.path.join(BACKUP_DIR, rel_dir)
                os.makedirs(dest_dir, exist_ok=True)
                
                temp_file = os.path.join(root, f)
                final_local_path = os.path.join(dest_dir, f)
                shutil.copy2(temp_file, final_local_path)
                log(f"Local backup saved at {final_local_path}")
        
        # Cleanup old backups
        retention = int(settings.get('retention_days', 7))
        cleanup_local_backups(retention)

    if settings.get('ftp_enabled', False):
        log("Uploading to FTP server...")
        try:
            host = settings.get('ftp_host')
            port = int(settings.get('ftp_port', 21))
            user = settings.get('ftp_user')
            password = settings.get('ftp_pass')
            remote_path = settings.get('ftp_path', '/')
            
            ftp = ftplib.FTP()
            ftp.connect(host, port)
            ftp.login(user, password)
            
            for root, dirs, files in os.walk(TEMP_DIR):
                for f in files:
                    rel_dir = os.path.relpath(root, TEMP_DIR).replace('\\', '/')
                    
                    # Create remote directories
                    current_path = remote_path
                    if not current_path.endswith('/'):
                        current_path += '/'
                        
                    ftp.cwd(current_path)
                    
                    if rel_dir != '.':
                        for part in rel_dir.split('/'):
                            try:
                                ftp.cwd(part)
                            except Exception:
                                ftp.mkd(part)
                                ftp.cwd(part)
                    
                    temp_file = os.path.join(root, f)
                    with open(temp_file, 'rb') as fp:
                        ftp.storbinary(f'STOR {f}', fp)
            ftp.quit()
            log("FTP upload completed successfully.")
        except Exception as e:
            log(f"FTP upload failed: {e}")

    if settings.get('s3_enabled', False):
        log("Uploading to S3 / DigitalOcean Spaces...")
        if not boto3:
            log("Error: boto3 is not installed. Run 'pip install boto3' first.")
        else:
            try:
                endpoint = settings.get('s3_endpoint')
                access_key = settings.get('s3_access_key')
                secret_key = settings.get('s3_secret_key')
                bucket = settings.get('s3_bucket')
                region = settings.get('s3_region')
                
                session = boto3.session.Session()
                client = session.client('s3',
                                        region_name=region,
                                        endpoint_url=endpoint,
                                        aws_access_key_id=access_key,
                                        aws_secret_access_key=secret_key)
                
                for root, dirs, files in os.walk(TEMP_DIR):
                    for f in files:
                        rel_dir = os.path.relpath(root, TEMP_DIR).replace('\\', '/')
                        s3_key = f"{rel_dir}/{f}" if rel_dir != '.' else f
                        temp_file = os.path.join(root, f)
                        client.upload_file(temp_file, bucket, s3_key)
                log("S3 upload completed successfully.")
            except Exception as e:
                log(f"S3 upload failed: {e}")

    # 5. Final Cleanup
    log("Cleaning up temporary files...")
    try:
        shutil.rmtree(TEMP_DIR)
    except Exception as e:
        log(f"Cleanup failed: {e}")
        
    log("Backup process finished.")

if __name__ == "__main__":
    run_backup()
