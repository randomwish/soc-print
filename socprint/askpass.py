"""SSH_ASKPASS helper: ssh runs this with the prompt as argv[1].

It forwards the prompt to the running soc-print server over its private unix
socket and prints the reply. Exiting non-zero tells ssh no answer is available.
Standard library only, so it starts fast and works under any Python.
"""

import os
import socket
import sys


def main() -> int:
    path = os.environ.get("SOC_PRINT_ASKPASS_SOCK")
    if not path:
        return 1
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(path)
            sock.sendall(prompt.encode("utf-8", "replace"))
            sock.shutdown(socket.SHUT_WR)
            reply = b""
            while chunk := sock.recv(4096):
                reply += chunk
    except OSError:
        return 1
    if not reply.startswith(b"\x01"):
        return 1
    sys.stdout.write(reply[1:].decode("utf-8") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
