import ctypes, sys, os, tempfile
from unittest import mock
d = tempfile.mkdtemp()
os.environ["FLOWSHIFT_LOG_DIR"] = d
os.environ["FLOWSHIFT_CONFIG"] = os.path.join(d, "config.json")
ctypes.windll = mock.MagicMock()
ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
sys.path.insert(0, "src/python")
import tray
print("ok", tray.peer_handler)
