"""Run the research harness on one SYNTHETIC world. Usage: harness_acceptance.py edge|null SEED [strategies]"""
import sys, time, tempfile, pathlib
from bot.config import *
from bot.data.synthetic import make_prices
from bot.backtest.walkforward import research, split_holdout
from bot.strategies import make_registry
edge = sys.argv[1]=="edge"; seed=int(sys.argv[2]); only=sys.argv[3].split(",") if len(sys.argv)>3 else None
u=load_universe(); risk=load_risk(); costs=load_costs(); cfg=load_strategy()
p=make_prices(seed=seed, planted_edge=edge)
dev,hold,cut=split_holdout(p,2)
dev={"close":dev["close"],"open":dev["open"],"cash":dev["cash"],"volume":dev["volume"]}
t=time.time()
d=pathlib.Path(tempfile.mkdtemp())
res=research(dev,u,risk,costs,make_registry(cfg),cfg.walkforward,d/"e.jsonl","SYNTHETIC",n_placebo=40,n_boot=200,only=only)
print("time",round(time.time()-t),"trials",res["n_trials"])
for n,o in res["outcomes"].items():
    print(n,o.verdict,o.final_params)
    for c in o.criteria: print("  ",c["id"],"PASS" if c["pass"] else "FAIL",c["value"])
