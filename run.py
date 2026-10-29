"""Lanzador robusto: localiza el paquete lce_bot estes como estes organizado el repo.

Cubre: lce_bot/ en la raiz, lce_bot/ dentro de una subcarpeta (p.ej. lce-bot/lce_bot/)
y archivos sueltos en la raiz. Si no encuentra nada, imprime el arbol del repo.
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
SKIP = {".git", ".venv", "node_modules", "__pycache__", "tests"}


def tree(root, depth=2):
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in SKIP and not d.startswith("."))
        rel = os.path.relpath(dp, root)
        if rel.count(os.sep) >= depth:
            dn[:] = []
        print(f"[run.py] {rel}/ -> {sorted(fn)}", flush=True)


def locate():
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in SKIP and not d.startswith(".")]
        if os.path.basename(dp) == "lce_bot" and "main.py" in fn:
            return "pkg", os.path.dirname(dp)
    if all(os.path.exists(os.path.join(ROOT, f)) for f in ("main.py", "strategy.py", "bingx.py")):
        return "flat", ROOT
    return None, None


def main():
    kind, where = locate()
    print(f"[run.py] layout={kind} dir={where}", flush=True)
    if kind is None:
        print("[run.py] ERROR: no encuentro lce_bot/main.py. Estructura del repo:", flush=True)
        tree(ROOT)
        sys.exit(1)
    if kind == "pkg":
        sys.path.insert(0, where)
    else:                                   # archivos sueltos: los trato como paquete lce_bot
        pkg = types.ModuleType("lce_bot")
        pkg.__path__ = [where]
        sys.modules["lce_bot"] = pkg
    from lce_bot.main import main as bot_main
    if os.getenv("LCE_CHECK"):
        print("[run.py] import OK", flush=True)
        return
    bot_main()


if __name__ == "__main__":
    main()
