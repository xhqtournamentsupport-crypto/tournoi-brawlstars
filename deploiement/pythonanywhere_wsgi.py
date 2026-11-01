# Fichier WSGI pour PythonAnywhere.
# Remplace TON_PSEUDO par ton nom d'utilisateur PythonAnywhere, puis colle tout ce fichier
# dans "Web" -> "WSGI configuration file".
import sys

CHEMIN = '/home/TON_PSEUDO/tournoi-brawlstars'
if CHEMIN not in sys.path:
    sys.path.insert(0, CHEMIN)

from app import app as application  # noqa: E402
