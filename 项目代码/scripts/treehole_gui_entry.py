import sys

from pku_treehole_digest.config import load_config
from pku_treehole_digest.gui import main, project_root


if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        load_config(project_root() / "config.yaml")
    else:
        main()
