import gi
gi.require_version('Camel', '1.2')
gi.require_version('EDataServer', '1.2')
from gi.repository import Camel, EDataServer, GLib
import os

home = os.path.expanduser("~")
os.environ['CAMEL_USER_DATA_DIR'] = os.path.join(home, ".local/share/evolution")
os.environ['CAMEL_USER_CACHE_DIR'] = os.path.join(home, ".cache/evolution")

session = Camel.Session()
# We need to set these properties to avoid the criticals or just to be sure
session.set_property("user-data-dir", os.environ['CAMEL_USER_DATA_DIR'])
session.set_property("user-cache-dir", os.environ['CAMEL_USER_CACHE_DIR'])

# Let's try to ref a service using a known UID
uid = "1c9edfdd5d8ffdd6b17c0e7dfb664ac1fee3e688"
print(f"Refing service {uid}")
try:
    service = session.ref_service(uid)
    print(f"Service: {service}")
except Exception as e:
    print(f"Failed to ref service: {e}")
