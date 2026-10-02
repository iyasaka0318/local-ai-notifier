"""Send one command to the phone to check the PC -> phone path.

Run with: ./runtime/run-worker.sh scripts/send_phone_test.py [action] [key=value ...]
With no arguments it sends a harmless "ping".
"""
import os
import sys

import _bootstrap  # noqa: F401
from phone_commands import load_key, send_phone_command

action = sys.argv[1] if len(sys.argv) > 1 else "ping"
fields = {}
for pair in sys.argv[2:]:
    name, _, value = pair.partition("=")
    fields[name] = int(value) if value.isdigit() else value
command = send_phone_command(
    os.environ["NTFY_TOPIC"], action, key=load_key(create=True), **fields
)
shown = {name: ("***" if name == "key" else value) for name, value in command.items()}
print("送信しました:", shown)
