import gi
gi.require_version('Camel', '1.2')
from gi.repository import Camel, GLib, GObject
import os

# Create the session
session = Camel.Session()

# Try setting the property
home = os.path.expanduser("~")
data_dir = os.path.join(home, ".local/share/evolution")

print(f"Setting user-data-dir to {data_dir}")
try:
    session.set_property("user-data-dir", data_dir)
    print("Success setting user-data-dir")
except Exception as e:
    print(f"Failed to set user-data-dir: {e}")

print(f"Current user-data-dir: {session.get_user_data_dir()}")
