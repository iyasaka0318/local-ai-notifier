"""Create the phone-command key and show what the Automate flow needs.

Run with: ./runtime/run-worker.sh scripts/phone_command_setup.py
The topic and the key are credentials: copy them to the phone, do not share them.
"""
import os

import _bootstrap  # noqa: F401
from phone_commands import load_key, phone_topic

print("ntfy アプリで購読するトピック:", phone_topic(os.environ["NTFY_TOPIC"]))
print("Automate の鍵:", load_key(create=True))
