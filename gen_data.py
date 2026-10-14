"""Generate a deliberately messy, synthetic GCP-like estate.

Outputs (in ./data):
  resources.json  inventory with utilisation, labels, dependencies and security flags
  billing.csv     90 days of daily cost by project and service
  budgets.json    monthly budget per project, set on the baseline from ~90 days ago

All data is synthetic. Nothing here touches a real cloud account.
In a real run, resources.json comes from Cloud Asset Inventory / Security Command Center
and billing.csv from the BigQuery billing export; the agents read the same fields.
"""
import csv
import datetime as dt
import json
import random
from collections import defaultdict
from pathlib import Path

SEED = 42
END = dt.date(2026, 10, 14)  # last day of simulated billing
DAYS = 90
DATA = Path(__file__).parent / "data"

# Cost growth over the 90 days, by project (ml-sandbox is the runaway one)
GROWTH = {"core-prod": 0.03, "data-platform": 0.02, "ml-sandbox": 0.30, "shared-services": 0.04}

SERVICE = {
    "compute": "Compute Engine", "cloudsql": "Cloud SQL", "gke": "GKE", "disk": "Persistent Disk",
    "snapshot": "Persistent Disk", "storage": "Cloud Storage", "bq": "BigQuery",
    "lb": "Networking", "address": "Networking", "firewall": "Networking",
}

PLAT = "platform-team@example.com"
DATA_ENG = "data-eng@example.com"
ML = "ml-team@example.com"
FIN = "finance-systems@example.com"
MKT = "marketing-tech@example.com"
SEC = "security-team@example.com"


def R(id, type, cost, project, owner, env, **kw):
    d = dict(id=id, type=type, monthly_cost=float(cost), project=project, owner=owner, env=env,
             cpu_util=50.0, last_used_days=0, attached=True, public=False, encrypted=True,
             labels={}, depends_on=[], sa_key_age_days=None, sa_roles=[],
             log_retention_days=None, compliance_retention_days=None, data_class=None)
    d.update(kw)
    return d


def build_resources():
    return [
        # core-prod
        R("prod-api-vm-01", "compute", 640, "core-prod", PLAT, "prod", cpu_util=55,
          depends_on=["orders-db-primary", "batch-vm-legacy"]),
        R("prod-api-vm-02", "compute", 640, "core-prod", PLAT, "prod", cpu_util=48,
          depends_on=["orders-db-primary"]),
        R("orders-db-primary", "cloudsql", 900, "core-prod", PLAT, "prod", cpu_util=52),
        R("orders-db-dr-replica", "cloudsql", 880, "core-prod", PLAT, "prod", cpu_util=1.5,
          labels={"dr": "true"}, depends_on=["orders-db-primary"]),
        R("prod-gke-nodepool", "gke", 2100, "core-prod", PLAT, "prod", cpu_util=22),
        R("lb-old-campaign", "lb", 95, "core-prod", MKT, "prod", cpu_util=0, last_used_days=90),
        R("staging-sql-01", "cloudsql", 380, "core-prod", PLAT, "staging", cpu_util=9),
        # data-platform
        R("batch-vm-legacy", "compute", 420, "data-platform", DATA_ENG, "prod", cpu_util=3,
          last_used_days=2),
        R("analytics-bq-reservation", "bq", 1500, "data-platform", DATA_ENG, "prod", cpu_util=70),
        R("disk-orphan-b", "disk", 96, "data-platform", DATA_ENG, "prod", attached=False,
          last_used_days=200, encrypted=False),
        R("snap-archive-2024", "snapshot", 85, "data-platform", DATA_ENG, "prod", last_used_days=300),
        # ml-sandbox
        R("gpu-training-pool", "compute", 2400, "ml-sandbox", ML, "dev", cpu_util=65),
        R("dev-gke-cluster", "gke", 700, "ml-sandbox", ML, "dev", cpu_util=12),
        R("dev-sandbox-vm-07", "compute", 210, "ml-sandbox", ML, "dev", cpu_util=2, last_used_days=45),
        R("dev-sandbox-vm-11", "compute", 210, "ml-sandbox", ML, "dev", cpu_util=1, last_used_days=60),
        R("disk-orphan-a", "disk", 48, "ml-sandbox", ML, "dev", attached=False, last_used_days=120),
        R("dev-firewall-open-ssh", "firewall", 0, "ml-sandbox", ML, "dev", public=True),
        # shared-services
        R("snap-archive-2023", "snapshot", 130, "shared-services", FIN, "shared",
          last_used_days=500, public=True),
        R("disk-orphan-c", "disk", 40, "shared-services", FIN, "shared", attached=False,
          last_used_days=75, labels={"legal_hold": "true"}),
        R("ip-unused-1", "address", 7.3, "shared-services", PLAT, "shared", attached=False,
          last_used_days=200),
        R("ip-unused-2", "address", 7.3, "shared-services", PLAT, "shared", attached=False,
          last_used_days=150),
        R("logs-bucket-audit", "storage", 520, "shared-services", SEC, "shared",
          log_retention_days=730, compliance_retention_days=365),
        R("logs-bucket-security", "storage", 310, "shared-services", SEC, "shared",
          log_retention_days=400, compliance_retention_days=400),
        R("pii-export-bucket", "storage", 60, "shared-services", FIN, "shared", public=True,
          data_class="pii"),
        R("sa-ci-legacy", "service_account", 0, "shared-services", PLAT, "shared",
          sa_key_age_days=540, sa_roles=["roles/owner"], last_used_days=130),
    ]


def growth_factor(project, i):
    """1.0 on the last day, lower earlier, so the current run-rate matches resource costs."""
    return 1 - GROWTH[project] * (DAYS - 1 - i) / (DAYS - 1)


def build_billing(resources):
    rng = random.Random(SEED)
    agg = defaultdict(float)
    start = END - dt.timedelta(days=DAYS - 1)
    for r in resources:
        if r["monthly_cost"] <= 0:
            continue
        svc = SERVICE[r["type"]]
        for i in range(DAYS):
            d = start + dt.timedelta(days=i)
            wk = 0.85 if (d.weekday() >= 5 and r["env"] in ("dev", "staging")
                          and r["type"] in ("compute", "gke", "cloudsql")) else 1.0
            spike = 1.6 if (r["project"] == "data-platform" and r["type"] == "bq" and 62 <= i <= 64) else 1.0
            noise = rng.gauss(1.0, 0.03)
            agg[(d, r["project"], svc)] += (r["monthly_cost"] / 30) * growth_factor(r["project"], i) * wk * spike * noise
    return agg


def main():
    DATA.mkdir(exist_ok=True)
    resources = build_resources()
    (DATA / "resources.json").write_text(json.dumps(resources, indent=2))

    agg = build_billing(resources)
    with open(DATA / "billing.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "project", "service", "cost_usd"])
        for (d, p, s), v in sorted(agg.items()):
            w.writerow([d.isoformat(), p, s, round(v, 2)])

    budgets = {}
    for p, g in GROWTH.items():
        run_rate = sum(r["monthly_cost"] for r in resources if r["project"] == p)
        budgets[p] = int(round(run_rate * growth_factor(p, 0) * 1.10, -2))
    (DATA / "budgets.json").write_text(json.dumps(budgets, indent=2))
    print(f"Generated {len(resources)} resources, {len(agg)} billing rows, budgets {budgets}")


if __name__ == "__main__":
    main()
