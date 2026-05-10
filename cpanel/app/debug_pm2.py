import subprocess
import os
import json

def debug_pm2():
    print(f"User: {os.getlogin() if hasattr(os, 'getlogin') else 'unknown'}")
    print(f"UID: {os.getuid()}")
    print(f"PATH: {os.environ.get('PATH')}")
    print(f"HOME: {os.environ.get('HOME')}")
    
    try:
        which_pm2 = subprocess.run(['which', 'pm2'], capture_output=True, text=True).stdout.strip()
        print(f"Which PM2: {which_pm2}")
        
        jlist = subprocess.run(['pm2', 'jlist'], capture_output=True, text=True).stdout.strip()
        print(f"PM2 JList Length: {len(jlist)}")
        print(f"PM2 JList Preview: {jlist[:100]}")
        
        # Check if pm2 is actually running
        ping = subprocess.run(['pm2', 'ping'], capture_output=True, text=True).stdout.strip()
        print(f"PM2 Ping: {ping}")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    debug_pm2()
