import os
import pty
import select
import termios
import struct
import fcntl
import threading
import json
from flask import session

def register_terminal_websocket(sock):
    @sock.route('/terminal/ws')
    def terminal_ws(ws):
        # Security: ensure user is logged in
        if not session.get('logged_in'):
            ws.close()
            return

        # Fork a new pseudoterminal
        (child_pid, fd) = pty.fork()
        if child_pid == 0:
            # We are in the child process.
            # Set the terminal environment
            os.environ["TERM"] = "xterm-256color"
            os.environ["HOME"] = "/root"
            
            # Execute bash as a login shell
            os.execv("/bin/bash", ["/bin/bash", "-l"])
        
        # We are in the parent process.
        
        # Start a background thread to read from the PTY and forward to the WebSocket
        def read_pty():
            try:
                while True:
                    # select() is used to avoid blocking indefinitely, though read() blocks
                    r, _, _ = select.select([fd], [], [])
                    if r:
                        out = os.read(fd, 1024 * 20)
                        if not out:
                            break
                        ws.send(out.decode('utf-8', 'replace'))
            except Exception:
                pass
            finally:
                # If reading stops, close the websocket
                try:
                    ws.close()
                except Exception:
                    pass

        t = threading.Thread(target=read_pty, daemon=True)
        t.start()

        # Main loop to read from the WebSocket and forward to the PTY
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                
                try:
                    msg = json.loads(data)
                    msg_type = msg.get('type')
                    
                    if msg_type == 'input':
                        # Write keystrokes to the PTY
                        os.write(fd, msg['data'].encode('utf-8'))
                    
                    elif msg_type == 'resize':
                        # Handle terminal resize
                        cols = int(msg.get('cols', 80))
                        rows = int(msg.get('rows', 24))
                        winsize = struct.pack("HHHH", rows, cols, 0, 0)
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            # Clean up the PTY process when the WebSocket disconnects
            try:
                os.close(fd)
            except Exception:
                pass
