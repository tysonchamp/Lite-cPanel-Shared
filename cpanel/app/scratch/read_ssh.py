import os
try:
    with open('/etc/ssh/sshd_config', 'r') as f:
        print(f.read())
except Exception as e:
    print(f"Error: {e}")
