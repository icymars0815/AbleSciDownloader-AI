"""python -m ablesci <command>  (the same as kytdl.cmd <command>)."""
import sys

from ablesci.cli.kytdl import main, safe_output

if __name__ == "__main__":
    safe_output()
    sys.exit(main())
