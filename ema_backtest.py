"""STRATEGY=ema + RUN_MODE=backtest: barrido de estrategias de EMA sobre muchos simbolos, neto de costes.

Variantes:
  cross : cruce de EMA rapida / lenta (siempre en mercado, invierte al cruzar)
  px20  : el precio cruza la EMA20; solo opera a favor de la tendencia (EMA20 vs EMA lenta), si no, cierra y espera
  filtros opcionales: ADX minimo en el cruce, stop de ATR (entrada +- k x ATR)
Las ordenes se ejecutan en la APERTURA de la vela siguiente a la senal. Coste = 2 x (FEE_PCT + SLIP_PCT) por operacion.
Resultado en % por operacion (no en R) para poder comparar variantes sin stop.
Validacion: train (primer 60 % del tiempo) para ordenar; test (ultimo 40 %) para confirmar.
"""
import json
import logging
import multiprocessing as mp
import time

import numpy as np

from . import indicators as ind
from .backtest import DAY, TRAIN_FRAC, covered_days, load_candles
from .config import clean_config
from .telegram import esc

log = logging.getLogger("ema")

FASTS = (9, 12, 20, 21, 34, 50, 100)
SLOWS = (21, 34, 50, 100, 200, 300)
PERIODS = sorted(set(FASTS) | set(SLOWS) | {20})


def ema_grid():
    out = []
    for slow in SLOWS:
        for fast in FASTS:
            if fast >= slow:
                continue
            for adx in (0, 20):
                for stop in (0, 3):
                    out.append({"mode": "cross", "fast": fast, "slow": slow, "adx": adx, "stop": stop})
    for slow in (50, 100, 200):
        for stop in (0, 3):
            out.append({"mode": "px20", "fast": 20, "slow": slow, "adx": 0, "stop": stop})
    return out


def cfg_name(c):
    base = f"EMA{c['fast']}/{c['slow']}" if c["mode"] == "cross" else f"Precio vs EMA20 (tendencia EMA{c['slow']})"
    return base + (f" · ADX>={c['adx']}" if c["adx"] else "") + (f" · stop {c['stop']}xATR" if c["stop"] else "")


def precompute(d):
    c = np.asarray(d["c"], float)
    return {"ema": {p: ind.ema(c, p) for p in PERIODS},
            "atr": ind.atr(d["h"], d["l"], d["c"], 14), "adx": ind.dmi(d["h"], d["l"], d["c"], 14)[2]}


def simulate(d, pre, cfg, cost_pct_side):
    """-> lista de operaciones cerradas (t_entrada, lado, bruto%, neto%, velas)."""
    t, o, h, l, c = (np.asarray(d[k]) for k in ("t", "o", "h", "l", "c"))
    n = len(c)
    ef, es = pre["ema"][cfg["fast"]], pre["ema"][cfg["slow"]]
    e20 = pre["ema"][20]
    atr, adx = pre["atr"], pre["adx"]
    cross = cfg["mode"] == "cross"
    cost = 2.0 * cost_pct_side
    trades = []
    pos = 0                      # +1 / -1 / 0
    entry = stop_px = 0.0
    e_bar = 0
    pending = None               # (accion, lado) a ejecutar en la apertura de la vela actual
    for i in range(1, n):
        # 1) ordenes pendientes de la senal de la vela anterior: se ejecutan a la apertura
        if pending is not None:
            want = pending
            pending = None
            if pos != 0 and want != pos:
                gross = ((o[i] - entry) / entry * 100.0) * pos
                trades.append((int(t[e_bar]), pos, gross, gross - cost, i - e_bar))
                pos = 0
            if pos == 0 and want != 0:
                pos, entry, e_bar = want, float(o[i]), i
                k = cfg["stop"]
                stop_px = entry - pos * k * atr[i - 1] if (k and not np.isnan(atr[i - 1])) else 0.0
        # 2) stop dentro de la vela
        if pos != 0 and stop_px:
            hit = (l[i] <= stop_px) if pos == 1 else (h[i] >= stop_px)
            if hit:
                px = min(o[i], stop_px) if pos == 1 else max(o[i], stop_px)
                gross = ((px - entry) / entry * 100.0) * pos
                trades.append((int(t[e_bar]), pos, gross, gross - cost, i - e_bar + 1))
                pos, stop_px = 0, 0.0
        # 3) senal al cierre de la vela i
        if np.isnan(es[i]) or np.isnan(es[i - 1]) or np.isnan(ef[i - 1]):
            continue
        if cross:
            up = ef[i - 1] <= es[i - 1] and ef[i] > es[i]
            dn = ef[i - 1] >= es[i - 1] and ef[i] < es[i]
            ok_up = ok_dn = True
        else:
            up = c[i - 1] <= e20[i - 1] and c[i] > e20[i]
            dn = c[i - 1] >= e20[i - 1] and c[i] < e20[i]
            ok_up, ok_dn = e20[i] > es[i], e20[i] < es[i]
        if cfg["adx"]:
            adx_ok = (not np.isnan(adx[i])) and adx[i] >= cfg["adx"]
            ok_up, ok_dn = ok_up and adx_ok, ok_dn and adx_ok
        if up:
            pending = 1 if ok_up else 0           # 0 = cerrar y esperar
        elif dn:
            pending = -1 if ok_dn else 0
    return trades


def metrics_pct(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0, "wr": None, "e": 0.0, "pf": 0.0, "tot": 0.0, "bars": 0.0}
    nets = np.array([r[3] for r in rows])
    gw, gl = nets[nets > 0].sum(), -nets[nets < 0].sum()
    return {"n": n, "wr": 100.0 * float((nets > 0).mean()), "e": float(nets.mean()), "pf": float(gw / gl) if gl > 0 else float("inf"),
            "tot": float(nets.sum()), "bars": float(np.mean([r[4] for r in rows]))}


def _eval_symbol(args):
    sym, d, cfgs, cost_side, window_start = args
    pre = precompute(d)
    out = {}
    for c in cfgs:
        out[cfg_name(c)] = [r for r in simulate(d, pre, c, cost_side) if r[0] >= window_start]
    return sym, out


def _fmt(m):
    wr = "s/d" if m["wr"] is None else f"{m['wr']:.0f}%"
    pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
    return f"n={m['n']} WR {wr} E {m['e']:+.3f}% PF {pf} ({m['bars']:.0f} velas)"


def passes(tr, te, min_n):
    return tr["n"] >= min_n and te["n"] >= max(20, min_n // 2) and tr["e"] > 0 and te["e"] > 0 and te["pf"] > 1.1


def evaluate(data, cfgs, cost_side, window_start, workers):
    tasks = [(s, d, cfgs, cost_side, window_start) for s, d in data.items()]
    agg = {cfg_name(c): [] for c in cfgs}
    if workers > 1 and len(tasks) > 1:
        with mp.get_context("fork").Pool(processes=workers) as pool:
            results = pool.map(_eval_symbol, tasks, chunksize=1)
    else:
        results = [_eval_symbol(t) for t in tasks]
    for _, out in results:
        for k, rows in out.items():
            agg[k].extend(rows)
    return agg


def run_backtest(bot):
    live_cfg, tg, bx = bot.cfg, bot.tg, bot.bx
    cfg = clean_config()
    for k in ("backtest_days", "backtest_symbols", "backtest_workers", "backtest_min_n"):
        setattr(cfg, k, getattr(live_cfg, k))
    days, nsym = cfg.backtest_days, cfg.backtest_symbols
    min_n = max(cfg.backtest_min_n, 50)
    bot.refresh_universe(force=True)
    syms = bot.universe[:nsym]
    tg.send(f"🔬 <b>Barrido EMA iniciado</b>: {cfg.timeframe} · {days} días · {len(syms)} símbolos · {len(ema_grid())} variantes. Descargando…")
    t0 = time.time()
    data = {}
    for sym in syms:
        try:
            d = load_candles(bx, cfg, sym, cfg.timeframe, days, extra_bars=400)
        except Exception as e:                                   # noqa: BLE001
            log.warning("descarga %s: %s", sym, e)
            d = None
        if d is not None:
            data[sym] = d
    if not data:
        tg.send("⛔ No pude descargar historial.")
        return
    cov = float(np.median([covered_days(d) for d in data.values()]))
    end_t = max(int(d["t"][-1]) for d in data.values())
    window_start = end_t - int(days * DAY)
    split_t = window_start + int(TRAIN_FRAC * days * DAY)
    cost_side = cfg.fee_pct + cfg.slip_pct
    cfgs = ema_grid()
    agg = evaluate(data, cfgs, cost_side, window_start, cfg.backtest_workers)
    scored = {}
    for k, rows in agg.items():
        scored[k] = (metrics_pct([r for r in rows if r[0] < split_t]), metrics_pct([r for r in rows if r[0] >= split_t]), metrics_pct(rows))
    ranked = sorted([k for k in scored if scored[k][0]["n"] >= min_n], key=lambda k: -scored[k][0]["e"])
    winners = [k for k in ranked if passes(scored[k][0], scored[k][1], min_n)]
    mine = "EMA100/300"
    lines = [f"🔬 <b>Barrido EMA</b> · {cfg.timeframe} · {cov:.0f} d · {len(data)} símbolos · {len(scored)} variantes",
             f"Coste {2 * cost_side:.2f}% por operación (ida y vuelta) · Train {int(TRAIN_FRAC * 100)}% / Test {int((1 - TRAIN_FRAC) * 100)}% · E = media neta por operación",
             "", f"<b>Tu 2EMA 100/300 (sin filtros)</b>"]
    if mine in scored:
        lines += ["  train " + _fmt(scored[mine][0]), "  test  " + _fmt(scored[mine][1])]
    for label, pred in (("Mejor cruce 2EMA", lambda k: k.startswith("EMA")), ("Mejor con EMA20 (precio vs EMA20)", lambda k: k.startswith("Precio"))):
        ks = [k for k in ranked if pred(k)]
        if ks:
            k = ks[0]
            lines += ["", f"<b>{label} (por train)</b>: {esc(k)}", "  train " + _fmt(scored[k][0]), "  test  " + _fmt(scored[k][1])]
    lines += ["", "<b>Top 6 por train</b>"]
    for i, k in enumerate(ranked[:6], 1):
        lines += [f"{i}) {esc(k)} {'✅' if k in winners else '✗'}", "   train " + _fmt(scored[k][0]), "   test  " + _fmt(scored[k][1])]
    if winners:
        lines += ["", f"✅ Pasan train Y test: {len(winners)}. La más estable: <b>{esc(max(winners, key=lambda k: min(scored[k][0]['e'], scored[k][1]['e'])))}</b>",
                  "⚠️ Pocas semanas y muchas variantes probadas: riesgo de sobreajuste. Confirma en otras fechas/símbolos."]
    else:
        lines += ["", "❌ <b>Ninguna variante es rentable neta de costes en train Y test.</b> No hay configuración que recomendar con estos datos."]
    tg.send("\n".join(lines))
    dump = {"days": cov, "timeframe": cfg.timeframe, "split_t": split_t, "cost_side": cost_side,
            "configs": {k: {"train": scored[k][0], "test": scored[k][1], "all": scored[k][2]} for k in scored}}
    tg.send_document("sweep_ema.json", json.dumps(dump, indent=1, default=str).encode(), "Resultados completos del barrido EMA")
    log.info("barrido EMA terminado en %.0fs", time.time() - t0)
